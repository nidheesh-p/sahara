"""LocalDriveClient — StorageBackend backed by one or more locally mounted drives."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from sahara.config import SaharaConfig
from sahara.storage.s3_client import ManifestConflictError, S3ClientError
from sahara.utils.hash import compute_sha256

__all__ = ["LocalDriveClient"]

logger = logging.getLogger(__name__)


class LocalDriveClient:
    """StorageBackend that stores files on one or more locally mounted drives.

    Every write goes to ALL configured drives; reads come from the first
    available drive. This gives independent per-drive redundancy without
    requiring OS-level RAID.
    """

    def __init__(self, config: SaharaConfig) -> None:
        if not config.drive_paths:
            raise S3ClientError(
                "drive_paths is empty. Configure at least one drive path "
                "in ~/.sahara/config.toml or via `sahara init`."
            )
        self._drives: list[Path] = [Path(p) for p in config.drive_paths]
        self._manifest_key = config.manifest_key

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, drive: Path, key: str) -> Path:
        """Absolute path for *key* on *drive*."""
        return drive / key

    def _first_available(self, key: str) -> Path:
        """Return path on first drive that has *key*, or raise S3ClientError."""
        for drive in self._drives:
            p = self._resolve(drive, key)
            if p.exists():
                return p
        raise S3ClientError(f"Object not found on any drive: {key}")

    def _atomic_op(self, dst: Path, writer: Callable[[Path], None]) -> None:
        """Write to *dst* atomically: call *writer(tmp)*, then rename tmp → dst."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp~")
        try:
            writer(tmp)
            tmp.replace(dst)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _atomic_write(self, dst: Path, data: bytes) -> None:
        self._atomic_op(dst, lambda tmp: tmp.write_bytes(data))

    def _atomic_copy(self, src: Path, dst: Path) -> None:
        self._atomic_op(dst, lambda tmp: shutil.copy2(str(src), str(tmp)))

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: Path,
        key: str,
        metadata: Optional[dict[str, str]] = None,
        storage_class: str = "STANDARD",
        encrypt_fn: Optional[Callable[[Path], tuple[Path, str]]] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Copy *local_path* to ALL drives under *key*. Returns SHA-256."""
        upload_path = local_path
        sha256: Optional[str] = None
        tmp_enc: Optional[Path] = None

        try:
            if encrypt_fn is not None:
                tmp_enc, sha256 = encrypt_fn(local_path)
                upload_path = tmp_enc

            sha256 = sha256 or compute_sha256(upload_path)
            file_size = upload_path.stat().st_size

            for drive in self._drives:
                dst = self._resolve(drive, key)
                self._atomic_copy(upload_path, dst)
                if on_progress:
                    on_progress(file_size)

        finally:
            if tmp_enc is not None:
                tmp_enc.unlink(missing_ok=True)

        return sha256  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_file(
        self,
        key: str,
        local_path: Path,
        decrypt_fn: Optional[Callable[[Path, Path], str]] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Copy *key* from first available drive to *local_path*. Returns SHA-256."""
        src = self._first_available(key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".tmp~")

        try:
            shutil.copy2(str(src), str(tmp))

            if decrypt_fn is not None:
                dec_tmp = local_path.with_suffix(local_path.suffix + ".dec~")
                try:
                    sha256 = decrypt_fn(tmp, dec_tmp)
                    tmp.unlink(missing_ok=True)
                    dec_tmp.replace(local_path)
                except Exception:
                    dec_tmp.unlink(missing_ok=True)
                    raise
            else:
                sha256 = compute_sha256(tmp)
                tmp.replace(local_path)

            if on_progress:
                on_progress(src.stat().st_size)

        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        return sha256

    # ------------------------------------------------------------------
    # Object management
    # ------------------------------------------------------------------

    def delete_object(self, key: str) -> None:
        """Delete *key* from ALL drives."""
        for drive in self._drives:
            p = self._resolve(drive, key)
            p.unlink(missing_ok=True)

    def copy_object(
        self,
        src_key: str,
        dst_key: str,
        storage_class: str = "STANDARD",
        extra_metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Copy *src_key* to *dst_key* on ALL drives. Returns SHA-256 of source."""
        src_sha: Optional[str] = None
        found = False

        for drive in self._drives:
            src = self._resolve(drive, src_key)
            dst = self._resolve(drive, dst_key)
            if not src.exists():
                logger.warning(
                    "copy_object: source '%s' missing on drive %s — skipping drive",
                    src_key,
                    drive,
                )
                continue
            found = True
            self._atomic_copy(src, dst)
            if src_sha is None:
                src_sha = compute_sha256(src)  # compute once from source

        if not found:
            raise S3ClientError(f"Source object not found on any drive: {src_key}")
        return src_sha  # type: ignore[return-value]

    def head_object(self, key: str) -> dict[str, Any]:
        """Return metadata for *key* from first available drive."""
        src = self._first_available(key)
        stat = src.stat()
        return {
            "ContentLength": stat.st_size,
            "ContentType": "application/octet-stream",
            "ETag": compute_sha256(src),
            "StorageClass": "STANDARD",
            "Restore": None,
            "Metadata": {},
            "LastModified": datetime.datetime.fromtimestamp(
                stat.st_mtime, tz=datetime.timezone.utc
            ),
        }

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def get_manifest(
        self,
        key: Optional[str] = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        """Read manifest from first available drive. Returns (dict, etag) or (None, None)."""
        manifest_key = key or self._manifest_key
        for drive in self._drives:
            p = self._resolve(drive, manifest_key)
            if p.exists():
                content = p.read_bytes()
                etag = hashlib.sha256(content).hexdigest()
                try:
                    return json.loads(content), etag
                except json.JSONDecodeError as exc:
                    raise S3ClientError(f"Manifest JSON is corrupt: {exc}") from exc
        return None, None

    def put_manifest(
        self,
        manifest_dict: dict,
        if_match_etag: Optional[str] = None,
        key: Optional[str] = None,
    ) -> str:
        """Write manifest to ALL drives with optimistic locking. Returns new etag."""
        manifest_key = key or self._manifest_key
        body = json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8")
        new_etag = hashlib.sha256(body).hexdigest()

        # Check conflicts on ALL drives BEFORE writing to any.
        # This keeps all drives consistent even if one raises ManifestConflictError.
        if if_match_etag is not None:
            for drive in self._drives:
                p = self._resolve(drive, manifest_key)
                if p.exists():
                    current_etag = hashlib.sha256(p.read_bytes()).hexdigest()
                    if current_etag != if_match_etag:
                        raise ManifestConflictError(current_etag)

        for drive in self._drives:
            p = self._resolve(drive, manifest_key)
            self._atomic_write(p, body)

        return new_etag

    # ------------------------------------------------------------------
    # Bootstrap listing
    # ------------------------------------------------------------------

    def list_all_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """Walk first drive and return file metadata. Used only when no manifest exists."""
        objects: list[dict] = []
        drive = self._drives[0]
        manifest_key = self._manifest_key

        for fpath in drive.rglob("*"):
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(drive).as_posix()
            # Skip Sahara internal files
            if rel == manifest_key or rel.startswith(".sahara/") or ".sahara/" in rel:
                continue
            if prefix and not rel.startswith(prefix):
                continue

            stat = fpath.stat()
            objects.append(
                {
                    "Key": rel,
                    "Size": stat.st_size,
                    "ETag": compute_sha256(fpath),
                    "StorageClass": "STANDARD",
                    "LastModified": datetime.datetime.fromtimestamp(
                        stat.st_mtime, tz=datetime.timezone.utc
                    ),
                }
            )

        return objects

    # ------------------------------------------------------------------
    # Connectivity / capability
    # ------------------------------------------------------------------

    def validate_bucket_access(self) -> None:
        """Check all drive paths exist and are writable."""
        for drive in self._drives:
            if not drive.exists():
                raise S3ClientError(
                    f"Drive path does not exist: {drive}. "
                    "Make sure the drive is mounted."
                )
            test = drive / ".sahara" / ".write_test"
            try:
                test.parent.mkdir(parents=True, exist_ok=True)
                test.write_bytes(b"sahara-write-test")
                test.unlink()
            except OSError as exc:
                raise S3ClientError(
                    f"Drive {drive} is not writable: {exc}"
                ) from exc

    def check_conditional_put_support(self) -> bool:
        """Always True — we implement optimistic locking via content-hash comparison."""
        return True

    def restore_object(self, key: str, days: int = 7, tier: str = "Bulk") -> None:
        raise S3ClientError(
            "restore_object is not supported in local drive mode. "
            "Files are always immediately accessible on your drives."
        )
