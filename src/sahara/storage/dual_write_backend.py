"""DualWriteBackend — writes to a primary and secondary StorageBackend.

Used for local+glacier mode: primary is LocalDriveClient (fast, always available),
secondary is S3Client pointing at AWS Glacier Deep Archive (cold disaster-recovery backup).

Reads always come from primary. Secondary failures are non-blocking — a warning is
logged but the operation succeeds as long as primary succeeds.

Glacier copies are intentionally NOT deleted when files are removed locally
(glacier_keep_deleted=True by default) so the cold archive acts as an immutable
safety net even after local deletions.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sahara.storage.backend import StorageBackend
from sahara.storage.s3_client import S3ClientError

__all__ = ["DualWriteBackend"]

logger = logging.getLogger(__name__)

_GLACIER_STORAGE_CLASS = "DEEP_ARCHIVE"


class DualWriteBackend:
    """Wraps a primary and secondary StorageBackend.

    - Reads  → primary only
    - Writes → primary first, then secondary (non-blocking on secondary failure)
    - Deletes → primary only (secondary / Glacier copy is kept as immutable archive)
    """

    def __init__(
        self,
        primary: StorageBackend,
        secondary: StorageBackend,
        glacier_keep_deleted: bool = True,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._glacier_keep_deleted = glacier_keep_deleted

    # ------------------------------------------------------------------
    # Upload — write to both backends
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
        etag = self._primary.upload_file(
            local_path, key, metadata, storage_class, encrypt_fn, on_progress
        )
        try:
            # Always use DEEP_ARCHIVE for the Glacier secondary
            self._secondary.upload_file(
                local_path, key, metadata, _GLACIER_STORAGE_CLASS, encrypt_fn
            )
        except Exception as exc:
            logger.warning(
                "Glacier secondary upload failed for '%s' (primary succeeded): %s",
                key,
                exc,
            )
        return etag

    # ------------------------------------------------------------------
    # Download — primary only
    # ------------------------------------------------------------------

    def download_file(
        self,
        key: str,
        local_path: Path,
        decrypt_fn: Callable[[Path, Path], str] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        return self._primary.download_file(key, local_path, decrypt_fn, on_progress)

    # ------------------------------------------------------------------
    # Delete — primary only (Glacier kept as immutable archive by default)
    # ------------------------------------------------------------------

    def delete_object(self, key: str) -> None:
        self._primary.delete_object(key)
        if not self._glacier_keep_deleted:
            try:
                self._secondary.delete_object(key)
            except Exception as exc:
                logger.warning(
                    "Glacier secondary delete failed for '%s': %s", key, exc
                )

    # ------------------------------------------------------------------
    # Copy — write to both backends
    # ------------------------------------------------------------------

    def copy_object(
        self,
        src_key: str,
        dst_key: str,
        storage_class: str = "STANDARD",
        extra_metadata: dict[str, str] | None = None,
    ) -> str:
        etag = self._primary.copy_object(src_key, dst_key, storage_class, extra_metadata)
        try:
            self._secondary.copy_object(
                src_key, dst_key, _GLACIER_STORAGE_CLASS, extra_metadata
            )
        except Exception as exc:
            logger.warning(
                "Glacier secondary copy failed '%s' → '%s': %s", src_key, dst_key, exc
            )
        return etag

    # ------------------------------------------------------------------
    # Manifest — primary only (Glacier does not participate in manifest)
    # ------------------------------------------------------------------

    def get_manifest(
        self, key: str | None = None
    ) -> tuple[dict | None, str | None]:
        return self._primary.get_manifest(key)

    def put_manifest(
        self,
        manifest_dict: dict,
        if_match_etag: str | None = None,
        key: str | None = None,
    ) -> str:
        return self._primary.put_manifest(manifest_dict, if_match_etag, key)

    # ------------------------------------------------------------------
    # Read-only operations — primary only
    # ------------------------------------------------------------------

    def list_all_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        return self._primary.list_all_objects(prefix)

    def head_object(self, key: str) -> dict[str, Any]:
        return self._primary.head_object(key)

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def validate_bucket_access(self) -> None:
        self._primary.validate_bucket_access()
        try:
            self._secondary.validate_bucket_access()
        except Exception as exc:
            raise S3ClientError(
                f"Glacier secondary backend validation failed: {exc}"
            ) from exc

    def check_conditional_put_support(self) -> bool:
        return self._primary.check_conditional_put_support()

    def restore_object(self, key: str, days: int = 7, tier: str = "Bulk") -> None:
        raise S3ClientError(
            "restore_object is not available in local+glacier mode. "
            "Files are always accessible on your local drives. "
            "For Glacier disaster-recovery retrieval, use the AWS Console or CLI directly."
        )
