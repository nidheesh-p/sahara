"""LocalDriveClient — StorageBackend backed by one or more locally mounted drives."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from filelock import FileLock

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
        root = drive.expanduser().resolve()
        candidate = (root / key).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise S3ClientError(f"Storage key escapes configured drive: {key}")
        return candidate

    def _first_available(self, key: str) -> Path:
        """Return path on first drive that has *key*, or raise S3ClientError."""
        for drive in self._drives:
            p = self._resolve(drive, key)
            if p.exists():
                return p
        raise S3ClientError(f"Object not found on any drive: {key}")

    def _open_key_fd(self, drive: Path, key: str) -> int:
        try:
            parent_fd, filename = self._open_parent_fd(drive, key, create=False)
        except (FileNotFoundError, OSError) as exc:
            raise S3ClientError(f"Object not found on drive: {key}") from exc
        try:
            fd = os.open(
                filename,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(fd)
                raise S3ClientError(f"Object is not a regular file: {key}")
            return fd
        except (FileNotFoundError, OSError) as exc:
            raise S3ClientError(f"Object not found on drive: {key}") from exc
        finally:
            os.close(parent_fd)

    def _first_available_fd(self, key: str) -> int:
        for drive in self._drives:
            try:
                return self._open_key_fd(drive, key)
            except S3ClientError:
                continue
        raise S3ClientError(f"Object not found on any drive: {key}")

    def _read_key_bytes(self, drive: Path, key: str) -> bytes:
        if os.name != "posix":
            return self._resolve(drive, key).read_bytes()
        fd = self._open_key_fd(drive, key)
        with os.fdopen(fd, "rb") as handle:
            return handle.read()

    @contextmanager
    def _manifest_lock(
        self,
        drive: Path,
        lock_hash: str,
    ) -> Iterator[None]:
        lock_key = f".sahara/locks/manifest-{lock_hash}.lock"
        if os.name != "posix":
            lock_path = self._resolve(drive, lock_key)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path)):
                yield
            return

        import fcntl

        try:
            parent_fd, filename = self._open_parent_fd(
                drive,
                lock_key,
                create=True,
            )
        except OSError as exc:
            raise S3ClientError("Unable to open shared manifest lock") from exc
        lock_fd = -1
        try:
            lock_fd = os.open(
                filename,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            if lock_fd >= 0:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            os.close(parent_fd)

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
        self._atomic_op(dst, lambda tmp: tmp.write_bytes(data))  # type: ignore[arg-type]

    def _atomic_copy(self, src: Path, dst: Path) -> None:
        self._atomic_op(dst, lambda tmp: shutil.copy2(str(src), str(tmp)))  # type: ignore[arg-type]

    @staticmethod
    def _key_parts(key: str) -> list[str]:
        if (
            not key
            or key.startswith(("/", "\\"))
            or "\\" in key
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise S3ClientError(f"Storage key escapes configured drive: {key}")
        return key.split("/")

    def _open_parent_fd(
        self,
        drive: Path,
        key: str,
        *,
        create: bool,
    ) -> tuple[int, str]:
        parts = self._key_parts(key)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open(drive.expanduser().resolve(), flags)
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

    def _atomic_copy_to_key(self, drive: Path, key: str, src: Path) -> None:
        if os.name != "posix":
            self._atomic_copy(src, self._resolve(drive, key))
            return
        with src.open("rb") as source:
            self._atomic_stream_to_key(drive, key, source)

    def _atomic_stream_to_key(
        self,
        drive: Path,
        key: str,
        source: BinaryIO,
        *,
        digest: Any | None = None,
    ) -> None:
        try:
            parent_fd, filename = self._open_parent_fd(drive, key, create=True)
        except OSError as exc:
            raise S3ClientError(
                f"Storage key escapes configured drive: {key}"
            ) from exc
        temp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
        temp_fd = -1
        try:
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(temp_fd, "wb") as target:
                temp_fd = -1
                while chunk := source.read(1024 * 1024):
                    if digest is not None:
                        digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.replace(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def _atomic_write_to_key(self, drive: Path, key: str, data: bytes) -> None:
        if os.name != "posix":
            self._atomic_write(self._resolve(drive, key), data)
            return

        try:
            parent_fd, filename = self._open_parent_fd(drive, key, create=True)
        except OSError as exc:
            raise S3ClientError(
                f"Storage key escapes configured drive: {key}"
            ) from exc
        temp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
        temp_fd = -1
        try:
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(temp_fd, "wb") as target:
                temp_fd = -1
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def _delete_key(self, drive: Path, key: str) -> None:
        if os.name != "posix":
            self._resolve(drive, key).unlink(missing_ok=True)
            return
        try:
            parent_fd, filename = self._open_parent_fd(drive, key, create=False)
        except FileNotFoundError:
            return
        try:
            try:
                os.unlink(filename, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(parent_fd)

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: Path,
        key: str,
        metadata: dict[str, str] | None = None,
        storage_class: str = "STANDARD",
        encrypt_fn: Callable[[Path], tuple[Path, str]] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Copy *local_path* to ALL drives under *key*. Returns SHA-256."""
        upload_path = local_path
        sha256: str | None = None
        tmp_enc: Path | None = None

        try:
            if encrypt_fn is not None:
                tmp_enc, sha256 = encrypt_fn(local_path)
                upload_path = tmp_enc

            sha256 = sha256 or compute_sha256(upload_path)
            file_size = upload_path.stat().st_size

            for drive in self._drives:
                self._atomic_copy_to_key(drive, key, upload_path)
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
        decrypt_fn: Callable[[Path, Path], str] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Copy *key* from first available drive to *local_path*. Returns SHA-256."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._secure_temp_path(local_path, "download")

        try:
            source_size = 0
            if os.name == "posix":
                source_fd = self._first_available_fd(key)
                source_size = os.fstat(source_fd).st_size
                with os.fdopen(source_fd, "rb") as source, tmp.open("wb") as target:
                    shutil.copyfileobj(source, target)
            else:
                src = self._first_available(key)
                source_size = src.stat().st_size
                shutil.copy2(str(src), str(tmp))

            if decrypt_fn is not None:
                dec_tmp = self._secure_temp_path(local_path, "decrypt")
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
                on_progress(source_size)

        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        return sha256

    @staticmethod
    def _secure_temp_path(local_path: Path, purpose: str) -> Path:
        """Create a unique mode-0600 sibling temporary file."""
        for _ in range(100):
            candidate = local_path.parent / (
                f".{local_path.name}.{purpose}.{uuid.uuid4().hex}.tmp"
            )
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            if os.name == "posix":
                flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
        raise S3ClientError("Unable to create a secure download temporary file")

    # ------------------------------------------------------------------
    # Object management
    # ------------------------------------------------------------------

    def delete_object(self, key: str) -> None:
        """Delete *key* from ALL drives."""
        for drive in self._drives:
            self._delete_key(drive, key)

    def copy_object(
        self,
        src_key: str,
        dst_key: str,
        storage_class: str = "STANDARD",
        extra_metadata: dict[str, str] | None = None,
    ) -> str:
        """Copy *src_key* to *dst_key* on ALL drives. Returns SHA-256 of source."""
        src_sha: str | None = None
        found = False

        for drive in self._drives:
            dst = self._resolve(drive, dst_key)
            try:
                if os.name == "posix":
                    source_fd = self._open_key_fd(drive, src_key)
                    source = os.fdopen(source_fd, "rb")
                else:
                    src = self._resolve(drive, src_key)
                    source = src.open("rb")
            except (S3ClientError, FileNotFoundError):
                logger.warning(
                    "copy_object: source '%s' missing on drive %s — skipping drive",
                    src_key,
                    drive,
                )
                continue
            with source:
                digest = hashlib.sha256()
                if os.name == "posix":
                    self._atomic_stream_to_key(
                        drive,
                        dst_key,
                        source,
                        digest=digest,
                    )
                else:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                    source.seek(0)
                    self._atomic_copy(src, dst)
            found = True
            if src_sha is None:
                src_sha = digest.hexdigest()

        if not found:
            raise S3ClientError(f"Source object not found on any drive: {src_key}")
        return src_sha  # type: ignore[return-value]

    def head_object(self, key: str) -> dict[str, Any]:
        """Return metadata for *key* from first available drive."""
        if os.name == "posix":
            source_fd = self._first_available_fd(key)
            stat = os.fstat(source_fd)
            digest = hashlib.sha256()
            with os.fdopen(source_fd, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            etag = digest.hexdigest()
        else:
            src = self._first_available(key)
            stat = src.stat()
            etag = compute_sha256(src)
        return {
            "ContentLength": stat.st_size,
            "ContentType": "application/octet-stream",
            "ETag": etag,
            "StorageClass": "STANDARD",
            "Restore": None,
            "Metadata": {},
            "LastModified": datetime.datetime.fromtimestamp(
                stat.st_mtime, tz=datetime.UTC
            ),
        }

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def get_manifest(
        self,
        key: str | None = None,
    ) -> tuple[dict | None, str | None]:
        """Read manifest from first available drive. Returns (dict, etag) or (None, None)."""
        manifest_key = key or self._manifest_key
        for drive in self._drives:
            try:
                content = self._read_key_bytes(drive, manifest_key)
            except (S3ClientError, FileNotFoundError):
                continue
            etag = hashlib.sha256(content).hexdigest()
            try:
                return json.loads(content), etag
            except json.JSONDecodeError as exc:
                raise S3ClientError(f"Manifest JSON is corrupt: {exc}") from exc
        return None, None

    def put_manifest(
        self,
        manifest_dict: dict,
        if_match_etag: str | None = None,
        key: str | None = None,
        if_none_match: bool = False,
    ) -> str:
        """Write manifest to ALL drives with optimistic locking. Returns new etag."""
        if if_match_etag is not None and if_none_match:
            raise ValueError("if_match_etag and if_none_match are mutually exclusive")
        manifest_key = key or self._manifest_key
        body = json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8")
        new_etag = hashlib.sha256(body).hexdigest()
        resolved_drives = sorted(
            {drive.expanduser().resolve() for drive in self._drives},
            key=str,
        )
        lock_hash = hashlib.sha256(
            manifest_key.encode("utf-8")
        ).hexdigest()[:24]
        with ExitStack() as locks:
            for drive in resolved_drives:
                locks.enter_context(self._manifest_lock(drive, lock_hash))
            previous: dict[Path, bytes | None] = {}
            for drive in resolved_drives:
                try:
                    previous[drive] = self._read_key_bytes(drive, manifest_key)
                except (S3ClientError, FileNotFoundError):
                    previous[drive] = None

            if if_none_match and any(
                content is not None for content in previous.values()
            ):
                current = next(
                    content for content in previous.values() if content is not None
                )
                raise ManifestConflictError(hashlib.sha256(current).hexdigest())

            if if_match_etag is not None:
                existing_contents = [
                    content for content in previous.values() if content is not None
                ]
                if not existing_contents:
                    raise ManifestConflictError("missing")
                for content in existing_contents:
                    current_etag = hashlib.sha256(content).hexdigest()
                    if current_etag != if_match_etag:
                        raise ManifestConflictError(current_etag)

            committed: list[Path] = []
            try:
                for drive in resolved_drives:
                    self._atomic_write_to_key(drive, manifest_key, body)
                    committed.append(drive)
            except Exception:
                for drive in reversed(committed):
                    old_content = previous[drive]
                    if old_content is None:
                        self._delete_key(drive, manifest_key)
                    else:
                        self._atomic_write_to_key(
                            drive,
                            manifest_key,
                            old_content,
                        )
                raise

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
            rel = fpath.relative_to(drive).as_posix()
            # Skip Sahara internal files
            if rel == manifest_key or rel.startswith(".sahara/") or ".sahara/" in rel:
                continue
            if prefix and not rel.startswith(prefix):
                continue
            if os.name == "posix":
                try:
                    source_fd = self._open_key_fd(drive, rel)
                except S3ClientError:
                    continue
                metadata = os.fstat(source_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    os.close(source_fd)
                    continue
                digest = hashlib.sha256()
                with os.fdopen(source_fd, "rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                etag = digest.hexdigest()
            else:
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                metadata = fpath.stat()
                etag = compute_sha256(fpath)
            objects.append(
                {
                    "Key": rel,
                    "Size": metadata.st_size,
                    "ETag": etag,
                    "StorageClass": "STANDARD",
                    "LastModified": datetime.datetime.fromtimestamp(
                        metadata.st_mtime, tz=datetime.UTC
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
            try:
                self._atomic_write_to_key(
                    drive,
                    ".sahara/.write_test",
                    b"sahara-write-test",
                )
                self._delete_key(drive, ".sahara/.write_test")
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
