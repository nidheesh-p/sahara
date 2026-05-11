"""Core sync engine for Sahara — three-way diff, conflict resolution, execution."""

from __future__ import annotations

import datetime
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import filelock

from sahara.config import SaharaConfig
from sahara.utils.hash import compute_sha256 as _compute_sha256
from sahara.utils.encryption import (
    decrypt_file,
    encrypt_file,
    derive_key,
    generate_salt,
    get_passphrase,
    EncryptionError,
)
from sahara.sync.ignore_rules import IgnoreRules
from sahara.models import (
    FileRecord,
    ManifestEntry,
    SyncOperation,
    SyncResult,
    StorageTier,
)
from sahara.storage.backend import StorageBackend
from sahara.storage.s3_client import (
    S3Client,
    ManifestConflictError,
    S3ClientError,
)
from sahara.storage.state_db import StateDB

__all__ = [
    "SyncEngine",
    "DiffResult",
    "ConflictStrategy",
]

logger = logging.getLogger(__name__)

_MAX_MANIFEST_RETRIES = 3
_CONFLICT_TOLERANCE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Diff result
# ---------------------------------------------------------------------------


@dataclass
class DiffResult:
    """Output of the three-way diff computation."""

    local_new: list[str] = field(default_factory=list)
    remote_new: list[str] = field(default_factory=list)
    local_modified: list[str] = field(default_factory=list)
    remote_modified: list[str] = field(default_factory=list)
    conflict: list[str] = field(default_factory=list)
    local_deleted: list[str] = field(default_factory=list)
    remote_deleted: list[str] = field(default_factory=list)
    # Renames: (old_path, new_path)
    local_moves: list[tuple[str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.local_new,
                self.remote_new,
                self.local_modified,
                self.remote_modified,
                self.conflict,
                self.local_deleted,
                self.remote_deleted,
                self.local_moves,
            ]
        )


ConflictStrategy = str  # "backup" | "local" | "remote" | "ask"


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _local_mtime_utc(path: Path) -> datetime.datetime:
    ts = path.stat().st_mtime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _ensure_aware(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# SyncEngine
# ---------------------------------------------------------------------------


class SyncEngine:
    """Orchestrates all sync operations for a configured Sahara folder."""

    def __init__(
        self,
        config: SaharaConfig,
        db: StateDB,
        s3: StorageBackend,
        ignore_rules: Optional[IgnoreRules] = None,
        sync_folder: Optional[Path] = None,
        s3_prefix: str = "",
    ) -> None:
        self._config = config
        self._db = db
        self._s3 = s3
        self._s3_prefix = s3_prefix
        self._sync_folder = sync_folder or config.get_sync_folder_path()
        self._ignore = ignore_rules or IgnoreRules(
            self._sync_folder,
            extra_patterns=config.exclude_patterns,
        )
        self._lock_path = self._sync_folder / ".sahara" / "sync.lock"
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_s3_key(self, relative_path: str) -> str:
        """Build full S3 key for a relative path, respecting global prefix and s3_prefix."""
        parts = []
        if self._config.prefix:
            parts.append(self._config.prefix.rstrip("/"))
        if self._s3_prefix:
            parts.append(self._s3_prefix.strip("/"))
        parts.append(relative_path)
        return "/".join(parts)

    def _get_manifest_key(self) -> str:
        """Return the S3 key for this folder's manifest."""
        if self._s3_prefix:
            safe = self._s3_prefix.replace("/", "-")
            return f".sahara/manifest-{safe}.json"
        return self._config.manifest_key

    # ------------------------------------------------------------------
    # Local scan
    # ------------------------------------------------------------------

    def _scan_local(self) -> dict[str, "LocalFileInfo"]:
        """Walk the sync folder and return metadata for every non-ignored file."""

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        result: dict[str, LocalFileInfo] = {}
        base = self._sync_folder

        for dirpath, dirnames, filenames in os.walk(base):
            dp = Path(dirpath)
            rel_dir = dp.relative_to(base).as_posix()

            # Prune ignored directories in-place
            dirnames[:] = [
                d
                for d in dirnames
                if not self._ignore.matches(
                    (rel_dir + "/" + d + "/").lstrip("/")
                )
            ]

            for fname in filenames:
                fpath = dp / fname
                rel_file = fpath.relative_to(base).as_posix()
                if self._ignore.matches(rel_file):
                    continue
                try:
                    stat = fpath.stat()
                except OSError:
                    continue
                result[rel_file] = LocalFileInfo(
                    path=fpath,
                    relative=rel_file,
                    mtime=datetime.datetime.fromtimestamp(
                        stat.st_mtime, tz=datetime.timezone.utc
                    ),
                    size=stat.st_size,
                )

        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Three-way diff
    # ------------------------------------------------------------------

    def _three_way_diff(
        self,
        local_files: dict,
        manifest: dict[str, ManifestEntry],
        db_records: dict[str, FileRecord],
    ) -> DiffResult:
        """Compute the three-way diff between local, manifest (remote), and DB (base).

        Categories:
          local_new      — exists locally, not in manifest, not in DB
          remote_new     — in manifest, not local, not in DB
          local_modified — local SHA != DB SHA, manifest SHA == DB SHA
          remote_modified— manifest SHA != DB SHA, local SHA == DB SHA
          conflict       — both sides changed relative to DB base
          local_deleted  — in DB, not local, still in manifest as of last sync
          remote_deleted — in manifest before, now absent, file still local
        """
        result = DiffResult()

        all_paths: set[str] = (
            set(local_files.keys()) | set(manifest.keys()) | set(db_records.keys())
        )

        for path in all_paths:
            in_local = path in local_files
            in_manifest = path in manifest
            in_db = path in db_records
            db_rec = db_records.get(path)

            if db_rec and db_rec.is_deleted:
                # Previously soft-deleted; treat as not-in-db
                in_db = False
                db_rec = None

            if in_local and not in_manifest and not in_db:
                result.local_new.append(path)

            elif in_manifest and not in_local and not in_db:
                result.remote_new.append(path)

            elif in_db and not in_local and in_manifest:
                result.local_deleted.append(path)

            elif in_db and in_local and not in_manifest:
                result.remote_deleted.append(path)

            elif in_local and in_manifest and in_db and db_rec:
                manifest_entry = manifest[path]
                db_sha = db_rec.sha256_checksum
                manifest_sha = manifest_entry.sha256

                # Compute local SHA only if manifest or db differs (lazy)
                local_sha: Optional[str] = None

                def get_local_sha() -> str:
                    nonlocal local_sha
                    if local_sha is None:
                        local_sha = _compute_sha256(local_files[path].path)
                    return local_sha

                local_changed = get_local_sha() != db_sha
                remote_changed = manifest_sha != db_sha

                if local_changed and not remote_changed:
                    result.local_modified.append(path)
                elif remote_changed and not local_changed:
                    result.remote_modified.append(path)
                elif local_changed and remote_changed:
                    if get_local_sha() == manifest_sha:
                        # Same result — skip
                        pass
                    else:
                        result.conflict.append(path)
                # else: no change

            elif in_local and in_manifest and not in_db:
                # Both exist but we have no DB record (first sync or reset)
                manifest_entry = manifest[path]
                local_sha = _compute_sha256(local_files[path].path)
                if local_sha != manifest_entry.sha256:
                    result.conflict.append(path)
                # else they match — nothing to do

        return result

    # ------------------------------------------------------------------
    # Rename detection
    # ------------------------------------------------------------------

    def _detect_renames(
        self,
        diff: DiffResult,
        local_files: dict,
        manifest: dict[str, ManifestEntry],
    ) -> DiffResult:
        """Match local-deleted + local-new pairs by SHA-256 to detect renames."""
        if not diff.local_deleted or not diff.local_new:
            return diff

        # Build SHA-256 -> path maps
        new_by_sha: dict[str, list[str]] = {}
        for path in diff.local_new:
            sha = _compute_sha256(local_files[path].path)
            new_by_sha.setdefault(sha, []).append(path)

        deleted_by_sha: dict[str, list[str]] = {}
        for path in diff.local_deleted:
            if path in manifest:
                sha = manifest[path].sha256
                deleted_by_sha.setdefault(sha, []).append(path)

        renamed_new: set[str] = set()
        renamed_deleted: set[str] = set()

        for sha, deleted_paths in deleted_by_sha.items():
            if sha not in new_by_sha:
                continue
            candidate_news = new_by_sha[sha]

            for old_path in deleted_paths:
                old_stem = Path(old_path).stem
                old_parent = str(Path(old_path).parent)

                # Tiebreaker: prefer same parent dir or same stem
                best: Optional[str] = None
                for new_path in candidate_news:
                    if new_path in renamed_new:
                        continue
                    new_parent = str(Path(new_path).parent)
                    new_stem = Path(new_path).stem

                    if best is None:
                        best = new_path
                    else:
                        # Prefer same directory
                        if new_parent == old_parent and str(
                            Path(best).parent
                        ) != old_parent:
                            best = new_path
                        # Then prefer same stem
                        elif (
                            new_stem == old_stem
                            and Path(best).stem != old_stem
                            and str(Path(best).parent) != old_parent
                        ):
                            best = new_path

                if best is not None and best not in renamed_new:
                    # Check for ambiguity: multiple deleted with same SHA
                    if deleted_by_sha[sha].count(old_path) == 1 and len(
                        [d for d in deleted_by_sha[sha] if d not in renamed_deleted]
                    ) == 1:
                        diff.local_moves.append((old_path, best))
                        renamed_new.add(best)
                        renamed_deleted.add(old_path)

        # Remove from local_new / local_deleted
        diff.local_new = [p for p in diff.local_new if p not in renamed_new]
        diff.local_deleted = [
            p for p in diff.local_deleted if p not in renamed_deleted
        ]

        return diff

    # ------------------------------------------------------------------
    # Conflict resolution
    # ------------------------------------------------------------------

    def _resolve_conflicts(
        self,
        diff: DiffResult,
        strategy: ConflictStrategy,
        result: SyncResult,
    ) -> tuple[list[str], list[str], list[str]]:
        """Resolve conflicts according to *strategy*.

        Returns (upload_paths, download_paths, skip_paths).
        """
        upload_paths: list[str] = []
        download_paths: list[str] = []
        skip_paths: list[str] = []

        for path in diff.conflict:
            if strategy == "local":
                upload_paths.append(path)
            elif strategy == "remote":
                download_paths.append(path)
            elif strategy == "backup":
                # Always backup local side — download remote, rename local
                backup_path = path + f".conflict-{_now_utc().strftime('%Y%m%dT%H%M%SZ')}"
                local_abs = self._sync_folder / path
                backup_abs = self._sync_folder / backup_path
                try:
                    import shutil

                    shutil.copy2(str(local_abs), str(backup_abs))
                    download_paths.append(path)
                    result.conflicts.append(
                        f"{path} (local backed up as {backup_path})"
                    )
                except OSError as exc:
                    logger.error("Failed to backup conflict file %s: %s", path, exc)
                    skip_paths.append(path)
            else:
                # "ask" or unknown — skip and let caller handle
                result.conflicts.append(path)
                skip_paths.append(path)

        return upload_paths, download_paths, skip_paths

    # ------------------------------------------------------------------
    # Execute a single upload operation
    # ------------------------------------------------------------------

    def _execute_upload(
        self,
        path: str,
        storage_class: str = "STANDARD",
    ) -> Optional[FileRecord]:
        """Upload *path* to S3 and return the updated FileRecord."""
        local_abs = self._sync_folder / path
        s3_key = self._get_s3_key(path)
        mtime = _local_mtime_utc(local_abs)

        try:
            if self._config.encryption_enabled:
                passphrase = get_passphrase()
                if not passphrase:
                    raise EncryptionError(
                        "No passphrase available. Run `sahara encryption setup`."
                    )
                salt = generate_salt()
                from sahara.utils.encryption import derive_key as _derive_key

                key = _derive_key(passphrase, salt)

                import tempfile as _tmp

                with _tmp.NamedTemporaryFile(
                    delete=False, suffix=".saha"
                ) as tf:
                    tmp_enc_path = Path(tf.name)

                try:
                    plaintext_sha = encrypt_file(
                        local_abs, tmp_enc_path, key, salt
                    )
                    metadata = {
                        "sahara-sha256": plaintext_sha,
                        "sahara-modified-at": mtime.isoformat(),
                        "sahara-encrypted": "1",
                        "sahara-salt": salt.hex(),
                    }
                    etag = self._s3.upload_file(
                        tmp_enc_path,
                        s3_key,
                        metadata=metadata,
                        storage_class=storage_class,
                    )
                finally:
                    tmp_enc_path.unlink(missing_ok=True)

                sha256 = plaintext_sha
            else:
                sha256 = _compute_sha256(local_abs)
                metadata = {
                    "sahara-sha256": sha256,
                    "sahara-modified-at": mtime.isoformat(),
                }
                etag = self._s3.upload_file(
                    local_abs,
                    s3_key,
                    metadata=metadata,
                    storage_class=storage_class,
                )

            now = _now_utc()
            size = local_abs.stat().st_size
            record = FileRecord(
                relative_path=path,
                sha256_checksum=sha256,
                size_bytes=size,
                tier=storage_class,  # type: ignore[arg-type]
                s3_etag=etag,
                last_sync_at=now,
                local_modified_at=mtime,
                remote_modified_at=mtime,
            )
            return record

        except Exception as exc:
            logger.error("Upload failed for %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # Execute a single download operation
    # ------------------------------------------------------------------

    def _execute_download(
        self,
        path: str,
        manifest_entry: ManifestEntry,
    ) -> Optional[FileRecord]:
        """Download *path* from S3 and return the updated FileRecord."""
        local_abs = self._sync_folder / path
        s3_key = self._get_s3_key(path)

        try:
            if self._config.encryption_enabled:
                passphrase = get_passphrase()
                if not passphrase:
                    raise EncryptionError(
                        "No passphrase available. Run `sahara encryption setup`."
                    )

                def decrypt_fn(src: Path, dst: Path) -> str:
                    from sahara.utils.encryption import (
                        _HEADER_LEN,
                        _MAGIC,
                        _SALT_LEN,
                        derive_key,
                        decrypt_file as _df,
                    )

                    with open(src, "rb") as fh:
                        header = fh.read(_HEADER_LEN)
                    if header[:4] != _MAGIC:
                        raise EncryptionError("Not a Sahara encrypted file.")
                    salt = header[5 : 5 + _SALT_LEN]
                    key = derive_key(passphrase, salt)
                    return _df(src, dst, key)

                sha256 = self._s3.download_file(s3_key, local_abs, decrypt_fn=decrypt_fn)
            else:
                sha256 = self._s3.download_file(s3_key, local_abs)

            # Verify SHA
            if sha256 != manifest_entry.sha256:
                logger.warning(
                    "SHA-256 mismatch for %s: expected %s, got %s",
                    path,
                    manifest_entry.sha256,
                    sha256,
                )

            now = _now_utc()
            mtime_str = manifest_entry.modified_at
            try:
                mtime = datetime.datetime.fromisoformat(mtime_str)
            except ValueError:
                mtime = now

            # Restore local mtime to match remote
            mtime_ts = mtime.timestamp()
            try:
                os.utime(str(local_abs), (mtime_ts, mtime_ts))
            except OSError:
                pass

            record = FileRecord(
                relative_path=path,
                sha256_checksum=sha256,
                size_bytes=manifest_entry.size,
                tier=manifest_entry.tier,
                s3_etag=manifest_entry.etag,
                last_sync_at=now,
                local_modified_at=_ensure_aware(mtime),
                remote_modified_at=_ensure_aware(mtime),
            )
            return record

        except Exception as exc:
            logger.error("Download failed for %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # Execute a single delete operation
    # ------------------------------------------------------------------

    def _execute_delete_remote(self, path: str) -> bool:
        s3_key = self._get_s3_key(path)
        try:
            self._s3.delete_object(s3_key)
            return True
        except Exception as exc:
            logger.error("Remote delete failed for %s: %s", path, exc)
            return False

    def _execute_delete_local(self, path: str) -> bool:
        local_abs = self._sync_folder / path
        try:
            local_abs.unlink(missing_ok=True)
            return True
        except Exception as exc:
            logger.error("Local delete failed for %s: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # Execute a server-side move
    # ------------------------------------------------------------------

    def _execute_move(self, old_path: str, new_path: str) -> Optional[FileRecord]:
        old_s3 = self._get_s3_key(old_path)
        new_s3 = self._get_s3_key(new_path)
        try:
            etag = self._s3.copy_object(old_s3, new_s3)
            self._s3.delete_object(old_s3)

            existing = self._db.get_file(old_path, s3_prefix=self._s3_prefix)
            now = _now_utc()
            local_abs = self._sync_folder / new_path

            sha256 = existing.sha256_checksum if existing else _compute_sha256(local_abs)
            size = local_abs.stat().st_size if local_abs.exists() else (existing.size_bytes if existing else 0)

            record = FileRecord(
                relative_path=new_path,
                sha256_checksum=sha256,
                size_bytes=size,
                tier=(existing.tier if existing else "STANDARD"),
                s3_etag=etag,
                last_sync_at=now,
                local_modified_at=_local_mtime_utc(local_abs) if local_abs.exists() else now,
                remote_modified_at=now,
            )
            return record
        except Exception as exc:
            logger.error("Move failed %s -> %s: %s", old_path, new_path, exc)
            return None

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------

    def _build_manifest_from_db(self) -> dict[str, dict]:
        """Rebuild the manifest dict from the DB for this folder's s3_prefix."""
        manifest: dict[str, dict] = {}
        for record in self._db.list_files(
            include_deleted=False, s3_prefix=self._s3_prefix
        ):
            entry = ManifestEntry(
                sha256=record.sha256_checksum,
                size=record.size_bytes,
                tier=record.tier,
                modified_at=record.remote_modified_at.isoformat(),
                etag=record.s3_etag,
            )
            manifest[record.relative_path] = entry.to_dict()
        return manifest

    def _write_manifest_with_retry(
        self,
        manifest: dict[str, dict],
        current_etag: Optional[str],
    ) -> None:
        """Write manifest to S3 with conditional PUT, retrying on 412."""
        mkey = self._get_manifest_key()
        for attempt in range(_MAX_MANIFEST_RETRIES):
            try:
                self._s3.put_manifest(manifest, if_match_etag=current_etag, key=mkey)
                return
            except ManifestConflictError as exc:
                if attempt == _MAX_MANIFEST_RETRIES - 1:
                    raise S3ClientError(
                        f"Failed to write manifest after {_MAX_MANIFEST_RETRIES} attempts "
                        "due to concurrent modifications."
                    ) from exc
                # Reload remote manifest and merge
                logger.warning(
                    "Manifest conflict on attempt %d; reloading…", attempt + 1
                )
                remote_manifest, current_etag = self._s3.get_manifest(key=mkey)
                if remote_manifest:
                    # Merge: local DB entries win (we just synced)
                    remote_manifest.update(manifest)
                    manifest = remote_manifest

    # ------------------------------------------------------------------
    # Bootstrap: build manifest from ListObjectsV2
    # ------------------------------------------------------------------

    def _bootstrap_manifest(self) -> dict[str, ManifestEntry]:
        """Used ONLY when no manifest exists yet — list S3 objects for this folder."""
        logger.info("No manifest found; bootstrapping from S3 listing…")
        # Build the S3 listing prefix scoped to this folder
        base_prefix = self._config.prefix.rstrip("/") + "/" if self._config.prefix else ""
        if self._s3_prefix:
            list_prefix = base_prefix + self._s3_prefix.strip("/") + "/"
        else:
            list_prefix = base_prefix

        objects = self._s3.list_all_objects(prefix=list_prefix)
        manifest: dict[str, ManifestEntry] = {}
        manifest_key = self._get_manifest_key()

        for obj in objects:
            key = obj["Key"]
            if key == manifest_key or key == self._config.manifest_key:
                continue
            # Strip the full listing prefix to get path relative to this folder
            if list_prefix and key.startswith(list_prefix):
                rel = key[len(list_prefix):]
            else:
                rel = key

            if not rel or rel.startswith(".sahara/") or ".sahara/" in rel:
                continue

            manifest[rel] = ManifestEntry(
                sha256="",  # Unknown until downloaded
                size=obj["Size"],
                tier=obj["StorageClass"],
                modified_at=obj["LastModified"].isoformat(),
                etag=obj["ETag"],
            )

        return manifest

    # ------------------------------------------------------------------
    # Main sync method
    # ------------------------------------------------------------------

    def sync(
        self,
        push_only: bool = False,
        pull_only: bool = False,
        dry_run: bool = False,
        verify: bool = False,
    ) -> SyncResult:
        """Execute a full sync cycle.

        Algorithm:
        1. Acquire advisory lock
        2. Fetch manifest from S3 (or bootstrap via ListObjectsV2 if absent)
        3. Scan local files
        4. Three-way diff (local, manifest, DB)
        5. Detect renames
        6. Resolve conflicts
        7. Execute operations via ThreadPoolExecutor with as_completed()
        8. Update DB within as_completed loop per operation
        9. Rebuild manifest from DB and write with conditional PUT
        10. Release lock
        """
        result = SyncResult()
        lock = filelock.FileLock(str(self._lock_path), timeout=30)

        try:
            lock.acquire()
        except filelock.Timeout:
            raise S3ClientError(
                "Another sync is already running for this folder. "
                "If this is incorrect, delete .sahara/sync.lock."
            )

        try:
            return self._sync_inner(push_only, pull_only, dry_run, verify, result)
        finally:
            lock.release()

    def _sync_inner(
        self,
        push_only: bool,
        pull_only: bool,
        dry_run: bool,
        verify: bool,
        result: SyncResult,
    ) -> SyncResult:
        # Step 2: Fetch manifest (per-folder manifest key)
        raw_manifest, manifest_etag = self._s3.get_manifest(key=self._get_manifest_key())

        if raw_manifest is None:
            manifest_entries = self._bootstrap_manifest()
        else:
            manifest_entries = {
                path: ManifestEntry.from_dict(data)
                for path, data in raw_manifest.items()
            }

        # Step 3: Scan local
        local_files = self._scan_local()

        # Step 4: Build DB records map
        db_records = {r.relative_path: r for r in self._db.list_files(include_deleted=True, s3_prefix=self._s3_prefix)}

        # Step 5: Three-way diff
        diff = self._three_way_diff(local_files, manifest_entries, db_records)

        # Step 6: Rename detection
        diff = self._detect_renames(diff, local_files, manifest_entries)

        # Step 7: Conflict resolution
        strategy = self._config.conflict_strategy
        conflict_uploads, conflict_downloads, conflict_skips = self._resolve_conflicts(
            diff, strategy, result
        )
        result.skipped.extend(conflict_skips)

        if dry_run:
            # Summarise without executing
            result.uploaded = diff.local_new + diff.local_modified + conflict_uploads
            result.downloaded = diff.remote_new + diff.remote_modified + conflict_downloads
            result.deleted = diff.local_deleted + diff.remote_deleted
            result.moved = diff.local_moves
            return result

        # Step 8: Build work items
        futures: dict[Future, tuple[str, str]] = {}  # future -> (op_type, path)

        with ThreadPoolExecutor(max_workers=self._config.max_workers) as executor:

            if not pull_only:
                for path in diff.local_new + diff.local_modified + conflict_uploads:
                    sc = self._config.default_storage_class
                    f = executor.submit(self._execute_upload, path, sc)
                    futures[f] = ("upload", path)

                for old_path, new_path in diff.local_moves:
                    f = executor.submit(self._execute_move, old_path, new_path)
                    futures[f] = ("move", f"{old_path}->{new_path}")

                if self._config.delete_remote_on_local_delete:
                    for path in diff.local_deleted:
                        f = executor.submit(self._execute_delete_remote, path)
                        futures[f] = ("delete_remote", path)

            if not push_only:
                for path in diff.remote_new + diff.remote_modified + conflict_downloads:
                    entry = manifest_entries.get(path)
                    if entry is None:
                        continue
                    f = executor.submit(self._execute_download, path, entry)
                    futures[f] = ("download", path)

                if self._config.delete_local_on_remote_delete:
                    for path in diff.remote_deleted:
                        f = executor.submit(self._execute_delete_local, path)
                        futures[f] = ("delete_local", path)

            # Step 9: Process results as they complete — update DB inline
            for future in as_completed(futures):
                op_type, path_info = futures[future]
                try:
                    op_result = future.result()
                except Exception as exc:
                    logger.error("Operation %s on %s failed: %s", op_type, path_info, exc)
                    result.failed.append((path_info, str(exc)))
                    continue

                if op_type == "upload":
                    if op_result is not None:
                        self._db.upsert_file(op_result, s3_prefix=self._s3_prefix)
                        self._db.add_history(
                            op_result.relative_path,
                            "upload",
                            sha256=op_result.sha256_checksum,
                            size_bytes=op_result.size_bytes,
                            s3_prefix=self._s3_prefix,
                        )
                        result.uploaded.append(op_result.relative_path)
                    else:
                        result.failed.append((path_info, "Upload returned no record"))

                elif op_type == "download":
                    if op_result is not None:
                        self._db.upsert_file(op_result, s3_prefix=self._s3_prefix)
                        self._db.add_history(
                            op_result.relative_path,
                            "download",
                            sha256=op_result.sha256_checksum,
                            size_bytes=op_result.size_bytes,
                            s3_prefix=self._s3_prefix,
                        )
                        result.downloaded.append(op_result.relative_path)
                    else:
                        result.failed.append((path_info, "Download returned no record"))

                elif op_type == "delete_remote":
                    if op_result:
                        self._db.mark_deleted(path_info, s3_prefix=self._s3_prefix)
                        self._db.add_history(path_info, "delete_remote", s3_prefix=self._s3_prefix)
                        result.deleted.append(path_info)
                    else:
                        result.failed.append((path_info, "Remote delete failed"))

                elif op_type == "delete_local":
                    if op_result:
                        self._db.mark_deleted(path_info, s3_prefix=self._s3_prefix)
                        self._db.add_history(path_info, "delete_local", s3_prefix=self._s3_prefix)
                        result.deleted.append(path_info)
                    else:
                        result.failed.append((path_info, "Local delete failed"))

                elif op_type.startswith("move"):
                    if op_result is not None:
                        old_p, new_p = path_info.split("->", 1)
                        self._db.delete_file(old_p, s3_prefix=self._s3_prefix)
                        self._db.upsert_file(op_result, s3_prefix=self._s3_prefix)
                        self._db.add_history(new_p, "move", details=f"from:{old_p}", s3_prefix=self._s3_prefix)
                        result.moved.append((old_p, new_p))
                    else:
                        result.failed.append((path_info, "Move failed"))

        # Step 10: Rebuild manifest and write
        new_manifest = self._build_manifest_from_db()
        try:
            self._write_manifest_with_retry(new_manifest, manifest_etag)
        except S3ClientError as exc:
            logger.error("Failed to write manifest: %s", exc)
            result.failed.append(("manifest", str(exc)))

        # Verification pass
        if verify and result.uploaded:
            for path in result.uploaded:
                s3_key = self._get_s3_key(path)
                try:
                    head = self._s3.head_object(s3_key)
                    db_rec = self._db.get_file(path, s3_prefix=self._s3_prefix)
                    if db_rec and head["Metadata"].get("sahara-sha256") != db_rec.sha256_checksum:
                        logger.warning("Verification failed for %s", path)
                except Exception as exc:
                    logger.warning("Verification check failed for %s: %s", path, exc)

        return result

    # ------------------------------------------------------------------
    # Status (no execution)
    # ------------------------------------------------------------------

    def get_status(self) -> DiffResult:
        """Return pending changes without executing any operations."""
        raw_manifest, _ = self._s3.get_manifest(key=self._get_manifest_key())

        if raw_manifest is None:
            manifest_entries = self._bootstrap_manifest()
        else:
            manifest_entries = {
                path: ManifestEntry.from_dict(data)
                for path, data in raw_manifest.items()
            }

        local_files = self._scan_local()
        db_records = {r.relative_path: r for r in self._db.list_files(include_deleted=True, s3_prefix=self._s3_prefix)}
        diff = self._three_way_diff(local_files, manifest_entries, db_records)
        diff = self._detect_renames(diff, local_files, manifest_entries)
        return diff

    # ------------------------------------------------------------------
    # Archive
    # ------------------------------------------------------------------

    def archive_files(
        self,
        paths: list[str],
        storage_class: str = "DEEP_ARCHIVE",
        dry_run: bool = False,
    ) -> list[str]:
        """Move files to Glacier/Deep Archive via server-side copy + delete original."""
        archived: list[str] = []
        for path in paths:
            s3_key = self._get_s3_key(path)
            if dry_run:
                archived.append(path)
                continue
            try:
                self._s3.copy_object(s3_key, s3_key, storage_class=storage_class)
                record = self._db.get_file(path, s3_prefix=self._s3_prefix)
                if record:
                    record.tier = storage_class  # type: ignore[assignment]
                    record.archived_at = _now_utc()
                    self._db.upsert_file(record, s3_prefix=self._s3_prefix)
                    self._db.add_history(path, "archive", details=storage_class, s3_prefix=self._s3_prefix)
                archived.append(path)
            except Exception as exc:
                logger.error("Archive failed for %s: %s", path, exc)
        return archived

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def request_restore(
        self,
        path: str,
        days: int = 7,
        tier: str = "Bulk",
    ) -> None:
        """Initiate a Glacier restore request."""
        s3_key = self._get_s3_key(path)
        self._s3.restore_object(s3_key, days=days, tier=tier)

        record = self._db.get_file(path, s3_prefix=self._s3_prefix)
        now = _now_utc()
        if record is None:
            record = FileRecord(
                relative_path=path,
                sha256_checksum="",
                size_bytes=0,
                tier="GLACIER",
                s3_etag="",
                last_sync_at=now,
                local_modified_at=now,
                remote_modified_at=now,
                restore_job_id=f"pending-{now.isoformat()}",
            )
        else:
            record.restore_job_id = f"pending-{now.isoformat()}"

        self._db.upsert_file(record, s3_prefix=self._s3_prefix)
        self._db.add_history(path, "restore_request", details=f"tier:{tier},days:{days}", s3_prefix=self._s3_prefix)

    def check_restore_status(self, path: str) -> dict:
        """Check S3 restore header and update DB accordingly."""
        s3_key = self._get_s3_key(path)
        head = self._s3.head_object(s3_key)
        restore_header = head.get("Restore", "")
        record = self._db.get_file(path, s3_prefix=self._s3_prefix)

        status = {
            "path": path,
            "tier": head.get("StorageClass", "UNKNOWN"),
            "restore_header": restore_header,
            "ready": False,
            "expires_at": None,
        }

        if restore_header:
            if 'ongoing-request="false"' in restore_header:
                status["ready"] = True
                # Parse expiry: expiry-date="Fri, 21 Dec 2012 00:00:00 GMT"
                import re

                m = re.search(r'expiry-date="([^"]+)"', restore_header)
                if m:
                    from email.utils import parsedate_to_datetime
                    try:
                        expiry = parsedate_to_datetime(m.group(1))
                        status["expires_at"] = expiry.isoformat()
                        if record:
                            record.restore_expires_at = expiry
                            record.tier = "HOT_TEMP"  # type: ignore[assignment]
                            record.restore_job_id = None
                            self._db.upsert_file(record, s3_prefix=self._s3_prefix)
                    except Exception:
                        pass

        return status

    def download_restored(self, path: str) -> Optional[str]:
        """Download a restored file from Glacier to the local sync folder."""
        status = self.check_restore_status(path)
        if not status["ready"]:
            return None

        entry = ManifestEntry(
            sha256="",
            size=0,
            tier=status["tier"],
            modified_at=_now_utc().isoformat(),
            etag="",
        )
        # Fetch real manifest entry if available
        raw_manifest, _ = self._s3.get_manifest(key=self._get_manifest_key())
        if raw_manifest and path in raw_manifest:
            entry = ManifestEntry.from_dict(raw_manifest[path])

        record = self._execute_download(path, entry)
        if record:
            self._db.upsert_file(record, s3_prefix=self._s3_prefix)
        return path if record else None
