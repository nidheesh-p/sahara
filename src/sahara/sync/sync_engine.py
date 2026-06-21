"""Core sync engine for Sahara — three-way diff, conflict resolution, execution."""

from __future__ import annotations

import datetime
import inspect
import logging
import os
import re
import stat
import tempfile
import unicodedata
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import filelock

from sahara.config import SaharaConfig
from sahara.models import (
    FileRecord,
    ManifestEntry,
    SyncResult,
)
from sahara.storage.backend import StorageBackend
from sahara.storage.s3_client import (
    ManifestConflictError,
    S3ClientError,
)
from sahara.storage.state_db import StateDB
from sahara.sync.ignore_rules import IgnoreRules
from sahara.utils.encryption import (
    EncryptionError,
    encrypt_file,
    generate_salt,
    get_passphrase,
)
from sahara.utils.hash import compute_sha256 as _compute_sha256

__all__ = [
    "SyncEngine",
    "DiffResult",
    "ConflictStrategy",
]

logger = logging.getLogger(__name__)

_CONFLICT_TOLERANCE_SECONDS = 2.0
_MAX_MANIFEST_RETRIES = 3
_STORAGE_TIERS = {
    "STANDARD",
    "GLACIER",
    "GLACIER_IR",
    "DEEP_ARCHIVE",
    "HOT_TEMP",
}
_SHA256_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*')


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


@dataclass
class LocalFileInfo:
    path: Path
    relative: str
    mtime: datetime.datetime
    size: int


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _local_mtime_utc(path: Path) -> datetime.datetime:
    ts = path.stat().st_mtime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _ensure_aware(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.UTC)
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
        ignore_rules: IgnoreRules | None = None,
        sync_folder: Path | None = None,
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
        self._unsupported_local_paths: set[str] = set()
        self._lock_path = self._sync_folder / ".sahara" / "sync.lock"
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_s3_key(self, relative_path: str) -> str:
        """Build full S3 key for a relative path, respecting global prefix and s3_prefix."""
        self._content_path_parts(relative_path)
        content_parts = [
            part
            for value in (self._config.prefix, self._s3_prefix, relative_path)
            for part in value.strip("/").split("/")
            if part
        ]
        if any(part.casefold() == ".sahara" for part in content_parts):
            raise S3ClientError(
                "Content paths cannot use Sahara's .sahara control namespace."
            )
        parts = []
        if self._config.prefix:
            parts.append(self._config.prefix.rstrip("/"))
        if self._s3_prefix:
            parts.append(self._s3_prefix.strip("/"))
        parts.append(relative_path)
        return "/".join(parts)

    @staticmethod
    def _content_path_parts(relative_path: str) -> tuple[str, ...]:
        """Validate a portable content path and return its POSIX components."""
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or relative_path.startswith(("/", "\\"))
            or "\\" in relative_path
            or PurePosixPath(relative_path).is_absolute()
            or PureWindowsPath(relative_path).is_absolute()
            or PureWindowsPath(relative_path).drive
        ):
            raise S3ClientError(f"Invalid content path in manifest: {relative_path!r}")
        parts = tuple(relative_path.split("/"))
        if any(
            not part
            or part in {".", ".."}
            or "\x00" in part
            for part in parts
        ):
            raise S3ClientError(f"Invalid content path in manifest: {relative_path!r}")
        if any(part.casefold() == ".sahara" for part in parts):
            raise S3ClientError(
                "Content paths cannot use Sahara's .sahara control namespace."
            )
        for part in parts:
            normalized = unicodedata.normalize("NFKC", part)
            device_name = normalized.split(".", 1)[0].upper()
            if (
                normalized in {".", ".."}
                or normalized.casefold() == ".sahara"
                or normalized.endswith((" ", "."))
                or "/" in normalized
                or "\\" in normalized
                or any(character in _WINDOWS_FORBIDDEN_CHARS for character in normalized)
                or any(ord(character) < 32 for character in normalized)
                or device_name in _WINDOWS_RESERVED_NAMES
            ):
                raise S3ClientError(
                    f"Content path is not portable across filesystems: {relative_path!r}"
                )
        return parts

    @classmethod
    def _content_path_identity(cls, relative_path: str) -> str:
        return "/".join(
            unicodedata.normalize("NFKC", part).casefold()
            for part in cls._content_path_parts(relative_path)
        )

    @classmethod
    def _validate_distinct_content_paths(cls, paths: set[str]) -> None:
        identities: dict[str, str] = {}
        for path in paths:
            identity = cls._content_path_identity(path)
            existing = identities.get(identity)
            if existing is not None and existing != path:
                raise S3ClientError(
                    f"Content paths alias across filesystems: {existing!r} and {path!r}"
                )
            identities[identity] = path

    @classmethod
    def _validate_three_way_content_paths(
        cls,
        local_paths: set[str],
        manifest_paths: set[str],
        db_paths: set[str],
    ) -> None:
        """Validate portable paths without blocking DB-backed case-only renames."""
        cls._validate_distinct_content_paths(local_paths)
        cls._validate_distinct_content_paths(manifest_paths)
        cls._validate_distinct_content_paths(db_paths)

        identity_paths: dict[str, set[str]] = {}
        for paths in (local_paths, manifest_paths, db_paths):
            for path in paths:
                identity_paths.setdefault(
                    cls._content_path_identity(path), set()
                ).add(path)

        for paths in identity_paths.values():
            if len(paths) <= 1:
                continue

            local_aliases = paths & local_paths
            manifest_aliases = paths & manifest_paths
            db_aliases = paths & db_paths
            is_db_backed_local_case_rename = (
                len(paths) == 2
                and len(local_aliases) == 1
                and len(manifest_aliases) == 1
                and manifest_aliases == db_aliases
                and not local_aliases & db_paths
            )
            if is_db_backed_local_case_rename:
                continue

            ordered = sorted(paths)
            raise S3ClientError(
                "Content paths alias across filesystems: "
                f"{ordered[0]!r} and {ordered[1]!r}"
            )

    def _manifest_entries(
        self,
        raw_manifest: dict,
        *,
        allow_unsupported_paths: bool = False,
        unsupported_paths: set[str] | None = None,
    ) -> dict[str, ManifestEntry]:
        """Parse a remote manifest only after validating every content path."""
        if not isinstance(raw_manifest, dict):
            raise S3ClientError("Remote manifest must be a JSON object.")
        if not all(isinstance(path, str) for path in raw_manifest):
            raise S3ClientError("Remote manifest paths must be text.")
        manifest_paths: list[str] = []
        if allow_unsupported_paths:
            identities: dict[str, str] = {}
            for path in raw_manifest:
                try:
                    identity = self._content_path_identity(path)
                except S3ClientError as exc:
                    if "not portable" not in str(exc):
                        raise
                    if unsupported_paths is not None:
                        unsupported_paths.add(path)
                    continue
                existing = identities.get(identity)
                if existing is not None and existing != path:
                    raise S3ClientError(
                        "Content paths alias across filesystems: "
                        f"{existing!r} and {path!r}"
                    )
                identities[identity] = path
                manifest_paths.append(path)
        else:
            self._validate_distinct_content_paths(set(raw_manifest))
            manifest_paths = list(raw_manifest)

        entries: dict[str, ManifestEntry] = {}
        for path in manifest_paths:
            data = raw_manifest[path]
            self._content_path_parts(path)
            if not isinstance(data, dict):
                raise S3ClientError(f"Invalid manifest entry for {path!r}")
            try:
                entry = ManifestEntry.from_dict(data)
                self._validate_manifest_entry(path, entry)
                entries[path] = entry
            except (KeyError, TypeError, ValueError) as exc:
                raise S3ClientError(
                    f"Invalid manifest entry for {path!r}"
                ) from exc
        return entries

    @staticmethod
    def _validate_manifest_entry(
        path: str,
        entry: ManifestEntry,
        *,
        allow_missing_checksum: bool = False,
    ) -> datetime.datetime:
        """Validate untrusted manifest metadata before any filesystem change."""
        if (
            not isinstance(entry.sha256, str)
            or not (
                _SHA256_RE.fullmatch(entry.sha256)
                or (allow_missing_checksum and not entry.sha256)
            )
            or type(entry.size) is not int
            or entry.size < 0
            or not isinstance(entry.tier, str)
            or entry.tier not in _STORAGE_TIERS
            or not isinstance(entry.modified_at, str)
            or not isinstance(entry.etag, str)
            or type(entry.ignored) is not bool
        ):
            raise S3ClientError(f"Invalid manifest entry for {path!r}")
        try:
            modified_at = datetime.datetime.fromisoformat(
                entry.modified_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise S3ClientError(
                f"Invalid manifest timestamp for {path!r}"
            ) from exc
        if modified_at.tzinfo is None:
            raise S3ClientError(
                f"Manifest timestamp lacks timezone for {path!r}"
            )
        return modified_at

    def _open_local_parent_fd(
        self,
        relative_path: str,
        *,
        create: bool,
    ) -> tuple[int, str]:
        """Open a destination parent beneath the sync root without following links."""
        parts = self._content_path_parts(relative_path)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open(self._sync_folder.resolve(), flags)
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd, parts[-1]
        except Exception:
            os.close(current_fd)
            raise

    def _install_download(self, staged_path: Path, relative_path: str) -> Path:
        """Atomically install a verified download beneath the sync root."""
        parts = self._content_path_parts(relative_path)
        if os.name != "posix":
            root = self._sync_folder.resolve()
            destination = root.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.parent.resolve().is_relative_to(root):
                raise S3ClientError(
                    f"Content path escapes sync folder: {relative_path!r}"
                )
            staged_path.replace(destination)
            return destination

        try:
            parent_fd, filename = self._open_local_parent_fd(
                relative_path,
                create=True,
            )
        except OSError as exc:
            raise S3ClientError(
                f"Content path has an unsafe parent: {relative_path!r}"
            ) from exc
        try:
            os.chmod(staged_path, 0o600)
            os.replace(staged_path, filename, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return self._sync_folder.joinpath(*parts)

    def _delete_local_path(self, relative_path: str) -> None:
        """Delete one local content path without following parent symlinks."""
        parts = self._content_path_parts(relative_path)
        if os.name != "posix":
            root = self._sync_folder.resolve()
            destination = root.joinpath(*parts)
            if not destination.parent.resolve().is_relative_to(root):
                raise S3ClientError(
                    f"Content path escapes sync folder: {relative_path!r}"
                )
            destination.unlink(missing_ok=True)
            return

        try:
            parent_fd, filename = self._open_local_parent_fd(
                relative_path,
                create=False,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise S3ClientError(
                f"Content path has an unsafe parent: {relative_path!r}"
            ) from exc
        try:
            try:
                os.unlink(filename, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(parent_fd)

    def _snapshot_local_file(
        self,
        relative_path: str,
    ) -> tuple[Path, str, datetime.datetime, int]:
        """Create a private, stable snapshot of one local regular file."""
        parts = self._content_path_parts(relative_path)
        source_fd = -1
        parent_fd = -1
        snapshot_fd = -1
        snapshot_path: Path | None = None
        source_filename: str | None = None
        try:
            if os.name == "posix":
                parent_fd, source_filename = self._open_local_parent_fd(
                    relative_path,
                    create=False,
                )
                source_fd = os.open(
                    source_filename,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent_fd,
                )
            else:
                source_path = self._sync_folder.joinpath(*parts)
                if source_path.is_symlink():
                    raise S3ClientError(
                        f"Local content path is not a regular file: {relative_path}"
                    )
                source_fd = os.open(source_path, os.O_RDONLY)

            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise S3ClientError(
                    f"Local content path is not a regular file: {relative_path}"
                )

            snapshot_fd, snapshot_name = tempfile.mkstemp(
                prefix=".sahara-upload-",
                dir=self._lock_path.parent,
            )
            snapshot_path = Path(snapshot_name)
            if os.name == "posix":
                os.fchmod(snapshot_fd, 0o600)
            with (
                os.fdopen(source_fd, "rb", closefd=False) as source,
                os.fdopen(snapshot_fd, "wb") as target,
            ):
                snapshot_fd = -1
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())

            after = os.fstat(source_fd)
            if os.name == "posix":
                if source_filename is None:
                    raise S3ClientError("Unable to verify upload snapshot")
                current_entry = os.stat(
                    source_filename,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            else:
                current_entry = os.stat(
                    self._sync_folder.joinpath(*parts),
                    follow_symlinks=False,
                )
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            ) or any(
                getattr(before, field) != getattr(current_entry, field)
                for field in ("st_dev", "st_ino")
            ) or not stat.S_ISREG(current_entry.st_mode):
                raise S3ClientError(
                    f"Local file changed while preparing upload: {relative_path}"
                )

            checksum = _compute_sha256(snapshot_path)
            modified_at = datetime.datetime.fromtimestamp(
                before.st_mtime,
                tz=datetime.UTC,
            )
            return snapshot_path, checksum, modified_at, before.st_size
        except (FileNotFoundError, OSError) as exc:
            if snapshot_path is not None:
                snapshot_path.unlink(missing_ok=True)
            raise S3ClientError(
                f"Unable to snapshot local file safely: {relative_path}"
            ) from exc
        except Exception:
            if snapshot_path is not None:
                snapshot_path.unlink(missing_ok=True)
            raise
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if snapshot_fd >= 0:
                os.close(snapshot_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    def _get_manifest_key(self) -> str:
        """Return the S3 key for this folder's manifest."""
        if self._s3_prefix:
            safe = urllib.parse.quote(self._s3_prefix, safe="")
            return f".sahara/manifest-{safe}.json"
        return self._config.manifest_key

    def _get_legacy_manifest_key(self) -> str | None:
        if not self._s3_prefix:
            return None
        safe = self._s3_prefix.replace("/", "-")
        legacy = f".sahara/manifest-{safe}.json"
        return legacy if legacy != self._get_manifest_key() else None

    def _get_manifest_with_legacy(
        self,
    ) -> tuple[dict | None, str | None, str, bool]:
        manifest_key = self._get_manifest_key()
        current_token = urllib.parse.quote(self._s3_prefix, safe="").casefold()
        if self._s3_prefix:
            other_prefixes = {
                *(
                    root["storage_prefix"]
                    for root in self._db.list_content_roots()
                ),
                *self._db.list_storage_ownership_prefixes(),
            }
            for prefix in other_prefixes:
                if prefix.casefold() == self._s3_prefix.casefold():
                    continue
                if current_token == prefix.replace("/", "-").casefold():
                    raise S3ClientError(
                        "This folder's manifest key aliases retained legacy "
                        f"ownership for storage prefix '{prefix}'."
                    )
        manifest, etag = self._s3.get_manifest(key=manifest_key)
        if manifest is not None:
            return manifest, etag, manifest_key, False

        legacy_key = self._get_legacy_manifest_key()
        if legacy_key is None:
            return None, None, manifest_key, True

        legacy_alias = self._s3_prefix.replace("/", "-").casefold()
        aliases = {
            prefix.replace("/", "-").casefold()
            for prefix in (
                *(
                    root["storage_prefix"]
                    for root in self._db.list_content_roots()
                ),
                *self._db.list_storage_ownership_prefixes(),
            )
            if prefix.casefold() != self._s3_prefix.casefold()
        }
        if legacy_alias in aliases:
            logger.warning(
                "Legacy manifest key %s is ambiguous; refusing automatic migration.",
                legacy_key,
            )
            return None, None, manifest_key, True
        if not self._db.list_files(s3_prefix=self._s3_prefix):
            logger.warning(
                "Legacy manifest key %s has no local ownership evidence; "
                "refusing automatic migration.",
                legacy_key,
            )
            return None, None, manifest_key, True

        legacy_manifest, legacy_etag = self._s3.get_manifest(key=legacy_key)
        if legacy_manifest is not None:
            logger.info(
                "Using legacy manifest %s for mixed-version compatibility.",
                legacy_key,
            )
            return legacy_manifest, legacy_etag, legacy_key, False
        return None, None, manifest_key, True

    # ------------------------------------------------------------------
    # Local scan
    # ------------------------------------------------------------------

    def _scan_local(self, *, clear_unsupported: bool = True) -> dict[str, LocalFileInfo]:
        """Walk the sync folder and return metadata for every non-ignored file."""
        result: dict[str, LocalFileInfo] = {}
        portable_identities: dict[str, str] = {}
        if clear_unsupported:
            self._unsupported_local_paths.clear()
        base = self._sync_folder

        for dirpath, dirnames, filenames in os.walk(base):
            dp = Path(dirpath)
            rel_dir = dp.relative_to(base).as_posix()

            # Prune ignored directories in-place
            safe_dirnames: list[str] = []
            for dirname in dirnames:
                try:
                    is_symlink = (dp / dirname).is_symlink()
                except OSError:
                    continue
                if not is_symlink and not self._ignore.matches(
                    (rel_dir + "/" + dirname + "/").lstrip("/")
                ):
                    safe_dirnames.append(dirname)
            dirnames[:] = safe_dirnames

            for fname in filenames:
                fpath = dp / fname
                rel_file = fpath.relative_to(base).as_posix()
                if self._ignore.matches(rel_file):
                    continue
                try:
                    identity = self._content_path_identity(rel_file)
                except S3ClientError:
                    self._unsupported_local_paths.add(rel_file)
                    continue
                aliased = portable_identities.get(identity)
                if aliased is not None and aliased != rel_file:
                    result.pop(aliased, None)
                    self._unsupported_local_paths.update({aliased, rel_file})
                    continue
                try:
                    metadata = fpath.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    self._unsupported_local_paths.add(rel_file)
                    continue
                portable_identities[identity] = rel_file
                result[rel_file] = LocalFileInfo(
                    path=fpath,
                    relative=rel_file,
                    mtime=datetime.datetime.fromtimestamp(
                        metadata.st_mtime, tz=datetime.UTC
                    ),
                    size=metadata.st_size,
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

        local_paths = set(local_files.keys()) - self._unsupported_local_paths
        manifest_paths = set(manifest.keys())
        active_db_paths: set[str] = set()
        for path, record in db_records.items():
            if record.is_deleted:
                continue
            try:
                self._content_path_identity(path)
            except S3ClientError:
                self._unsupported_local_paths.add(path)
                continue
            active_db_paths.add(path)
        all_paths: set[str] = local_paths | manifest_paths | active_db_paths
        self._validate_three_way_content_paths(
            local_paths,
            manifest_paths,
            active_db_paths,
        )

        for path in all_paths:
            if path in self._unsupported_local_paths:
                continue
            self._content_path_parts(path)
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
                residency = self._db.get_storage_residency(
                    self._s3_prefix, path
                )
                if not (
                    isinstance(residency, dict)
                    and residency.get("local_state") == "offloaded"
                ):
                    result.local_deleted.append(path)

            elif in_db and in_local and not in_manifest:
                result.remote_deleted.append(path)

            elif in_local and in_manifest and in_db and db_rec:
                manifest_entry = manifest[path]
                db_sha = db_rec.sha256_checksum
                manifest_sha = manifest_entry.sha256

                # Compute local SHA only if manifest or db differs (lazy)
                local_sha: str | None = None

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
                best: str | None = None
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
                snapshot: Path | None = None
                try:
                    snapshot, _, _, _ = self._snapshot_local_file(path)
                    self._install_download(snapshot, backup_path)
                    download_paths.append(path)
                    result.conflicts.append(
                        f"{path} (local backed up as {backup_path})"
                    )
                except (OSError, S3ClientError) as exc:
                    logger.error("Failed to backup conflict file %s: %s", path, exc)
                    skip_paths.append(path)
                finally:
                    if snapshot is not None:
                        snapshot.unlink(missing_ok=True)
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
    ) -> FileRecord | None:
        """Upload *path* to S3 and return the updated FileRecord."""
        s3_key = self._get_s3_key(path)
        snapshot_path: Path | None = None

        try:
            snapshot_path, sha256, mtime, size = self._snapshot_local_file(path)
            if self._config.encryption_enabled:
                passphrase = get_passphrase()
                if not passphrase:
                    raise EncryptionError(
                        "No passphrase available. Run `sahara encryption setup`."
                    )
                salt = generate_salt()
                from sahara.utils.encryption import derive_key as _derive_key

                key = _derive_key(passphrase, salt)

                encrypted_fd, encrypted_name = tempfile.mkstemp(
                    prefix=".sahara-upload-",
                    suffix=".saha",
                    dir=self._lock_path.parent,
                )
                os.close(encrypted_fd)
                tmp_enc_path = Path(encrypted_name)
                if os.name == "posix":
                    os.chmod(tmp_enc_path, 0o600)

                try:
                    plaintext_sha = encrypt_file(
                        snapshot_path, tmp_enc_path, key, salt
                    )
                    if plaintext_sha != sha256:
                        raise S3ClientError(
                            f"Upload snapshot checksum changed unexpectedly: {path}"
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
                metadata = {
                    "sahara-sha256": sha256,
                    "sahara-modified-at": mtime.isoformat(),
                }
                etag = self._s3.upload_file(
                    snapshot_path,
                    s3_key,
                    metadata=metadata,
                    storage_class=storage_class,
                )

            now = _now_utc()
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
        finally:
            if snapshot_path is not None:
                snapshot_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Execute a single download operation
    # ------------------------------------------------------------------

    def _execute_download(
        self,
        path: str,
        manifest_entry: ManifestEntry,
    ) -> FileRecord | None:
        """Download *path* from S3 and return the updated FileRecord."""
        self._content_path_parts(path)
        s3_key = self._get_s3_key(path)
        staged_path: Path | None = None

        try:
            mtime = self._validate_manifest_entry(
                path,
                manifest_entry,
                allow_missing_checksum=True,
            )
            stage_fd, stage_name = tempfile.mkstemp(
                prefix=".sahara-download-",
                dir=self._lock_path.parent,
            )
            os.close(stage_fd)
            staged_path = Path(stage_name)
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
                    )
                    from sahara.utils.encryption import (
                        decrypt_file as _df,
                    )

                    with open(src, "rb") as fh:
                        header = fh.read(_HEADER_LEN)
                    if header[:4] != _MAGIC:
                        raise EncryptionError("Not a Sahara encrypted file.")
                    salt = header[5 : 5 + _SALT_LEN]
                    key = derive_key(passphrase, salt)
                    return _df(src, dst, key)

                self._s3.download_file(
                    s3_key,
                    staged_path,
                    decrypt_fn=decrypt_fn,
                )
            else:
                self._s3.download_file(s3_key, staged_path)

            sha256 = _compute_sha256(staged_path)
            if manifest_entry.sha256 and sha256 != manifest_entry.sha256:
                raise S3ClientError(
                    f"SHA-256 mismatch for {path}: expected "
                    f"{manifest_entry.sha256}, got {sha256}"
                )

            local_abs = self._install_download(staged_path, path)
            now = _now_utc()

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
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

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
        try:
            self._delete_local_path(path)
            return True
        except Exception as exc:
            logger.error("Local delete failed for %s: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # Execute a server-side move
    # ------------------------------------------------------------------

    def _execute_move(self, old_path: str, new_path: str) -> FileRecord | None:
        old_s3 = self._get_s3_key(old_path)
        new_s3 = self._get_s3_key(new_path)
        snapshot: Path | None = None
        try:
            existing = self._db.get_file(old_path, s3_prefix=self._s3_prefix)
            try:
                snapshot, local_sha, local_mtime, local_size = (
                    self._snapshot_local_file(new_path)
                )
            except S3ClientError:
                try:
                    self._sync_folder.joinpath(
                        *self._content_path_parts(new_path)
                    ).lstat()
                except FileNotFoundError:
                    if existing is None:
                        raise
                else:
                    raise
                local_sha = existing.sha256_checksum
                local_mtime = _now_utc()
                local_size = existing.size_bytes

            if existing is None or local_sha != existing.sha256_checksum:
                if snapshot is not None:
                    snapshot.unlink(missing_ok=True)
                snapshot = None
                uploaded = self._execute_upload(
                    new_path,
                    existing.tier if existing else "STANDARD",
                )
                if uploaded is None:
                    return None
                self._s3.delete_object(old_s3)
                return uploaded

            etag = self._s3.copy_object(old_s3, new_s3)
            self._s3.delete_object(old_s3)

            now = _now_utc()

            record = FileRecord(
                relative_path=new_path,
                sha256_checksum=local_sha,
                size_bytes=local_size,
                tier=(existing.tier if existing else "STANDARD"),
                s3_etag=etag,
                last_sync_at=now,
                local_modified_at=local_mtime,
                remote_modified_at=now,
            )
            return record
        except Exception as exc:
            logger.error("Move failed %s -> %s: %s", old_path, new_path, exc)
            return None
        finally:
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------

    def _build_manifest_from_db(self) -> dict[str, dict]:
        """Rebuild the manifest dict from the DB for this folder's s3_prefix."""
        manifest: dict[str, dict] = {}
        for record in self._db.list_files(
            include_deleted=False, s3_prefix=self._s3_prefix
        ):
            self._content_path_parts(record.relative_path)
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
        current_etag: str | None,
        *,
        if_none_match: bool = False,
        manifest_key: str | None = None,
        base_manifest: dict[str, dict] | None = None,
    ) -> None:
        """Write a manifest delta with conditional retries."""
        mkey = manifest_key or self._get_manifest_key()
        desired_manifest = manifest
        original_manifest = base_manifest if base_manifest is not None else manifest
        deleted_paths = set(original_manifest) - set(desired_manifest)
        changed_entries = {
            path: data
            for path, data in desired_manifest.items()
            if original_manifest.get(path) != data
        }

        for attempt in range(_MAX_MANIFEST_RETRIES):
            try:
                if if_none_match:
                    put_manifest = self._s3.put_manifest
                    try:
                        parameters = inspect.signature(put_manifest).parameters
                        supports_create_only = (
                            "if_none_match" in parameters
                            or any(
                                parameter.kind
                                is inspect.Parameter.VAR_KEYWORD
                                for parameter in parameters.values()
                            )
                        )
                    except (TypeError, ValueError):
                        supports_create_only = False
                    if not supports_create_only:
                        raise S3ClientError(
                            "Storage backend does not support atomic create-only "
                            "manifest writes. Upgrade the backend implementation."
                        )
                    put_manifest(  # type: ignore[call-arg]
                        manifest,
                        if_match_etag=current_etag,
                        key=mkey,
                        if_none_match=True,
                    )
                else:
                    self._s3.put_manifest(
                        manifest,
                        if_match_etag=current_etag,
                        key=mkey,
                    )
                return
            except ManifestConflictError as exc:
                if attempt == _MAX_MANIFEST_RETRIES - 1:
                    raise S3ClientError(
                        "Failed to write manifest after conditional retries."
                    ) from exc
                remote_manifest, current_etag = self._s3.get_manifest(key=mkey)
                if remote_manifest is None:
                    manifest = {}
                    if_none_match = True
                    current_etag = None
                else:
                    if current_etag is None:
                        raise S3ClientError(
                            "Storage backend returned a manifest without an ETag."
                        )
                    self._manifest_entries(
                        remote_manifest,
                        allow_unsupported_paths=True,
                        unsupported_paths=self._unsupported_local_paths,
                    )
                    manifest = dict(remote_manifest)
                    if_none_match = False
                for path in deleted_paths:
                    manifest.pop(path, None)
                manifest.update(changed_entries)

    # ------------------------------------------------------------------
    # Bootstrap: build manifest from ListObjectsV2
    # ------------------------------------------------------------------

    def _bootstrap_manifest(
        self,
        *,
        unsupported_paths: set[str] | None = None,
    ) -> dict[str, ManifestEntry]:
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
            try:
                self._content_path_parts(rel)
            except S3ClientError as exc:
                if "not portable" not in str(exc):
                    raise
                if unsupported_paths is not None:
                    unsupported_paths.add(rel)
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
        self._unsupported_local_paths.clear()

        # Step 2: Fetch manifest (per-folder manifest key)
        raw_manifest, manifest_etag, manifest_key, manifest_needs_create = (
            self._get_manifest_with_legacy()
        )

        if raw_manifest is None:
            manifest_entries = self._bootstrap_manifest(
                unsupported_paths=self._unsupported_local_paths
            )
        else:
            manifest_entries = self._manifest_entries(
                raw_manifest,
                allow_unsupported_paths=True,
                unsupported_paths=self._unsupported_local_paths,
            )
        if raw_manifest is None:
            new_manifest = {
                path: entry.to_dict()
                for path, entry in manifest_entries.items()
            }
        else:
            new_manifest = dict(raw_manifest)
        base_manifest = dict(new_manifest)

        # Step 3: Scan local
        local_files = self._scan_local(clear_unsupported=False)

        # Step 4: Build DB records map
        db_records = {r.relative_path: r for r in self._db.list_files(include_deleted=True, s3_prefix=self._s3_prefix)}

        # Step 5: Three-way diff
        diff = self._three_way_diff(local_files, manifest_entries, db_records)
        result.skipped.extend(sorted(self._unsupported_local_paths))

        # Step 6: Rename detection
        diff = self._detect_renames(diff, local_files, manifest_entries)

        # Step 7: Conflict resolution
        strategy = self._config.conflict_strategy
        conflict_uploads, conflict_downloads, conflict_skips = self._resolve_conflicts(
            diff, strategy, result
        )
        result.skipped.extend(conflict_skips)
        if push_only:
            result.skipped.extend(
                diff.remote_new + diff.remote_modified + conflict_downloads
            )
        if pull_only:
            result.skipped.extend(
                diff.local_new
                + diff.local_modified
                + conflict_uploads
                + diff.local_deleted
                + [new_path for _, new_path in diff.local_moves]
            )

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
                        f = executor.submit(self._execute_delete_remote, path)  # type: ignore[arg-type]
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
                        f = executor.submit(self._execute_delete_local, path)  # type: ignore[arg-type]
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
                        new_manifest[op_result.relative_path] = ManifestEntry(
                            sha256=op_result.sha256_checksum,
                            size=op_result.size_bytes,
                            tier=op_result.tier,
                            modified_at=op_result.remote_modified_at.isoformat(),
                            etag=op_result.s3_etag,
                        ).to_dict()
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
                        new_manifest[op_result.relative_path] = ManifestEntry(
                            sha256=op_result.sha256_checksum,
                            size=op_result.size_bytes,
                            tier=op_result.tier,
                            modified_at=op_result.remote_modified_at.isoformat(),
                            etag=op_result.s3_etag,
                        ).to_dict()
                    else:
                        result.failed.append((path_info, "Download returned no record"))

                elif op_type == "delete_remote":
                    if op_result:
                        self._db.mark_deleted(path_info, s3_prefix=self._s3_prefix)
                        self._db.add_history(path_info, "delete_remote", s3_prefix=self._s3_prefix)
                        result.deleted.append(path_info)
                        new_manifest.pop(path_info, None)
                    else:
                        result.failed.append((path_info, "Remote delete failed"))

                elif op_type == "delete_local":
                    if op_result:
                        self._db.mark_deleted(path_info, s3_prefix=self._s3_prefix)
                        self._db.add_history(path_info, "delete_local", s3_prefix=self._s3_prefix)
                        result.deleted.append(path_info)
                        new_manifest.pop(path_info, None)
                    else:
                        result.failed.append((path_info, "Local delete failed"))

                elif op_type.startswith("move"):
                    if op_result is not None:
                        old_p, new_p = path_info.split("->", 1)
                        self._db.delete_file(old_p, s3_prefix=self._s3_prefix)
                        self._db.upsert_file(op_result, s3_prefix=self._s3_prefix)
                        self._db.add_history(new_p, "move", details=f"from:{old_p}", s3_prefix=self._s3_prefix)
                        result.moved.append((old_p, new_p))
                        new_manifest.pop(old_p, None)
                        new_manifest[new_p] = ManifestEntry(
                            sha256=op_result.sha256_checksum,
                            size=op_result.size_bytes,
                            tier=op_result.tier,
                            modified_at=op_result.remote_modified_at.isoformat(),
                            etag=op_result.s3_etag,
                        ).to_dict()
                    else:
                        result.failed.append((path_info, "Move failed"))

        # Step 10: Persist the fetched manifest plus successful operation deltas.
        # Bootstrap entries have no trusted checksum until downloaded. Do not create
        # a manifest that the strict reader would reject on the next sync.
        unresolved_bootstrap = manifest_needs_create and any(
            not _SHA256_RE.fullmatch(entry.get("sha256", ""))
            for entry in new_manifest.values()
        )
        if unresolved_bootstrap:
            logger.warning(
                "Deferring manifest creation until all bootstrapped objects "
                "have verified checksums."
            )
        else:
            try:
                self._write_manifest_with_retry(
                    new_manifest,
                    manifest_etag,
                    if_none_match=manifest_needs_create,
                    manifest_key=manifest_key,
                    base_manifest=base_manifest,
                )
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
        raw_manifest, _, _, _ = self._get_manifest_with_legacy()

        if raw_manifest is None:
            manifest_entries = self._bootstrap_manifest()
        else:
            manifest_entries = self._manifest_entries(raw_manifest)

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

    def download_restored(self, path: str) -> str | None:
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
        raw_manifest, _, _, _ = self._get_manifest_with_legacy()
        if raw_manifest and path in raw_manifest:
            entry = ManifestEntry.from_dict(raw_manifest[path])

        record = self._execute_download(path, entry)
        if record:
            self._db.upsert_file(record, s3_prefix=self._s3_prefix)
        return path if record else None
