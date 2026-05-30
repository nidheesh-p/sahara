"""Data models for Sahara."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "FileRecord",
    "SyncOperation",
    "ManifestEntry",
    "SyncResult",
    "StorageTier",
    "TIER_LABELS",
]

StorageTier = Literal["STANDARD", "GLACIER", "GLACIER_IR", "DEEP_ARCHIVE", "HOT_TEMP"]

# Human-friendly tier names
TIER_LABELS: dict[str, str] = {
    "GLACIER_IR": "Normal",
    "STANDARD": "Premium",
    "DEEP_ARCHIVE": "Archive",
    "GLACIER": "Glacier (Flex)",
    "HOT_TEMP": "Restored",
}

OpType = Literal[
    "upload",
    "download",
    "delete",
    "archive",
    "restore",
    "move",
    "skip",
]


@dataclass
class FileRecord:
    """Represents a file tracked in the local SQLite state database."""

    relative_path: str
    sha256_checksum: str
    size_bytes: int
    tier: StorageTier
    s3_etag: str
    last_sync_at: datetime.datetime
    local_modified_at: datetime.datetime
    remote_modified_at: datetime.datetime
    archived_at: datetime.datetime | None = None
    restore_job_id: str | None = None
    restore_expires_at: datetime.datetime | None = None
    is_deleted: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.last_sync_at, str):
            self.last_sync_at = datetime.datetime.fromisoformat(self.last_sync_at)
        if isinstance(self.local_modified_at, str):
            self.local_modified_at = datetime.datetime.fromisoformat(
                self.local_modified_at
            )
        if isinstance(self.remote_modified_at, str):
            self.remote_modified_at = datetime.datetime.fromisoformat(
                self.remote_modified_at
            )
        if isinstance(self.archived_at, str):
            self.archived_at = datetime.datetime.fromisoformat(self.archived_at)
        if isinstance(self.restore_expires_at, str):
            self.restore_expires_at = datetime.datetime.fromisoformat(
                self.restore_expires_at
            )


@dataclass
class SyncOperation:
    """Represents a single sync operation to be performed."""

    op_type: OpType
    path: str
    source_path: str | None = None
    dest_path: str | None = None
    size_bytes: int = 0
    sha256: str | None = None
    conflict_reason: str | None = None
    storage_class: str = "STANDARD"
    dry_run: bool = False

    def __repr__(self) -> str:
        return (
            f"SyncOperation(op_type={self.op_type!r}, path={self.path!r}, "
            f"size_bytes={self.size_bytes})"
        )


@dataclass
class ManifestEntry:
    """Represents a file entry in the S3 manifest JSON."""

    sha256: str
    size: int
    tier: StorageTier
    modified_at: str  # ISO-8601 string
    etag: str
    ignored: bool = False

    def to_dict(self) -> dict:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "tier": self.tier,
            "modified_at": self.modified_at,
            "etag": self.etag,
            "ignored": self.ignored,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ManifestEntry:
        return cls(
            sha256=data["sha256"],
            size=data["size"],
            tier=data.get("tier", "STANDARD"),
            modified_at=data["modified_at"],
            etag=data["etag"],
            ignored=data.get("ignored", False),
        )


@dataclass
class SyncResult:
    """Aggregated results from a sync operation."""

    uploaded: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    moved: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return (
            len(self.uploaded)
            + len(self.downloaded)
            + len(self.deleted)
            + len(self.moved)
        )

    @property
    def had_errors(self) -> bool:
        return len(self.failed) > 0

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if self.uploaded:
            lines.append(f"  Uploaded:    {len(self.uploaded)} file(s)")
        if self.downloaded:
            lines.append(f"  Downloaded:  {len(self.downloaded)} file(s)")
        if self.deleted:
            lines.append(f"  Deleted:     {len(self.deleted)} file(s)")
        if self.moved:
            lines.append(f"  Moved:       {len(self.moved)} file(s)")
        if self.skipped:
            lines.append(f"  Skipped:     {len(self.skipped)} file(s)")
        if self.conflicts:
            lines.append(f"  Conflicts:   {len(self.conflicts)} file(s)")
        if self.failed:
            lines.append(f"  Failed:      {len(self.failed)} file(s)")
        if not lines:
            lines.append("  Everything up to date.")
        return lines
