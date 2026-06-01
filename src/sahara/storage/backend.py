"""StorageBackend Protocol — the interface all Sahara storage backends implement."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["StorageBackend"]


@runtime_checkable
class StorageBackend(Protocol):
    """Structural interface for Sahara storage backends.

    Implemented by S3Client (AWS / MinIO), LocalDriveClient, and DualWriteBackend.
    SyncEngine accepts any StorageBackend rather than a concrete S3Client.
    """

    def upload_file(
        self,
        local_path: Path,
        key: str,
        metadata: dict[str, str] | None = None,
        storage_class: str = "STANDARD",
        encrypt_fn: Callable[[Path], tuple[Path, str]] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Upload local_path to storage under *key*. Returns an etag/hash string."""
        ...

    def download_file(
        self,
        key: str,
        local_path: Path,
        decrypt_fn: Callable[[Path, Path], str] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        """Download *key* from storage to local_path. Returns SHA-256 of the file."""
        ...

    def delete_object(self, key: str) -> None:
        """Delete *key* from storage."""
        ...

    def copy_object(
        self,
        src_key: str,
        dst_key: str,
        storage_class: str = "STANDARD",
        extra_metadata: dict[str, str] | None = None,
    ) -> str:
        """Copy *src_key* to *dst_key*. Returns etag/hash of the destination."""
        ...

    def get_manifest(
        self,
        key: str | None = None,
    ) -> tuple[dict | None, str | None]:
        """Fetch the Sahara manifest. Returns (manifest_dict, etag) or (None, None)."""
        ...

    def put_manifest(
        self,
        manifest_dict: dict,
        if_match_etag: str | None = None,
        key: str | None = None,
    ) -> str:
        """Write manifest. Raises ManifestConflictError on concurrent modification."""
        ...

    def list_all_objects(self, prefix: str = "") -> list[dict[str, Any]]:
        """List all objects under *prefix*. Used only for bootstrap when no manifest exists."""
        ...

    def head_object(self, key: str) -> dict[str, Any]:
        """Return metadata dict for *key* (ContentLength, ETag, StorageClass, etc.)."""
        ...

    def validate_bucket_access(self) -> None:
        """Raise S3ClientError if the backend is unreachable or access is denied."""
        ...

    def check_conditional_put_support(self) -> bool:
        """Return True if the backend supports atomic conditional manifest writes."""
        ...

    def restore_object(
        self,
        key: str,
        days: int = 7,
        tier: str = "Bulk",
    ) -> None:
        """Initiate a Glacier restore. Raises S3ClientError if not supported."""
        ...
