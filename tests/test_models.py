"""Tests for sahara.models."""
from __future__ import annotations

import datetime

import pytest

from sahara.models import (
    FileRecord,
    ManifestEntry,
    SyncOperation,
    SyncResult,
)


# ---------------------------------------------------------------------------
# FileRecord
# ---------------------------------------------------------------------------

NOW = datetime.datetime(2024, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_record(**kwargs) -> FileRecord:
    defaults = dict(
        relative_path="docs/report.pdf",
        sha256_checksum="abc123",
        size_bytes=1024,
        tier="STANDARD",
        s3_etag="etag-abc",
        last_sync_at=NOW,
        local_modified_at=NOW,
        remote_modified_at=NOW,
    )
    defaults.update(kwargs)
    return FileRecord(**defaults)


class TestFileRecord:
    def test_creation_basic(self):
        rec = _make_record()
        assert rec.relative_path == "docs/report.pdf"
        assert rec.sha256_checksum == "abc123"
        assert rec.size_bytes == 1024
        assert rec.tier == "STANDARD"
        assert rec.is_deleted is False
        assert rec.archived_at is None
        assert rec.restore_job_id is None
        assert rec.restore_expires_at is None

    def test_post_init_string_to_datetime_conversion(self):
        iso = "2024-01-15T12:00:00+00:00"
        rec = FileRecord(
            relative_path="test.txt",
            sha256_checksum="sha",
            size_bytes=0,
            tier="STANDARD",
            s3_etag="etag",
            last_sync_at=iso,
            local_modified_at=iso,
            remote_modified_at=iso,
        )
        assert isinstance(rec.last_sync_at, datetime.datetime)
        assert isinstance(rec.local_modified_at, datetime.datetime)
        assert isinstance(rec.remote_modified_at, datetime.datetime)

    def test_post_init_archived_at_string(self):
        iso = "2024-06-01T00:00:00+00:00"
        rec = _make_record(archived_at=iso)
        assert isinstance(rec.archived_at, datetime.datetime)

    def test_post_init_restore_expires_at_string(self):
        iso = "2024-06-10T00:00:00+00:00"
        rec = _make_record(restore_expires_at=iso)
        assert isinstance(rec.restore_expires_at, datetime.datetime)

    def test_none_optional_fields(self):
        rec = _make_record(archived_at=None, restore_job_id=None, restore_expires_at=None)
        assert rec.archived_at is None
        assert rec.restore_job_id is None
        assert rec.restore_expires_at is None

    def test_is_deleted_default(self):
        rec = _make_record()
        assert rec.is_deleted is False

    def test_is_deleted_true(self):
        rec = _make_record(is_deleted=True)
        assert rec.is_deleted is True


# ---------------------------------------------------------------------------
# SyncOperation
# ---------------------------------------------------------------------------


class TestSyncOperation:
    def test_repr(self):
        op = SyncOperation(op_type="upload", path="some/file.txt", size_bytes=512)
        r = repr(op)
        assert "upload" in r
        assert "some/file.txt" in r
        assert "512" in r

    def test_defaults(self):
        op = SyncOperation(op_type="delete", path="x.txt")
        assert op.size_bytes == 0
        assert op.sha256 is None
        assert op.conflict_reason is None
        assert op.storage_class == "STANDARD"
        assert op.dry_run is False

    def test_optional_fields(self):
        op = SyncOperation(
            op_type="move",
            path="old.txt",
            source_path="old.txt",
            dest_path="new.txt",
            sha256="deadbeef",
            conflict_reason="both changed",
            storage_class="GLACIER",
            dry_run=True,
        )
        assert op.source_path == "old.txt"
        assert op.dest_path == "new.txt"
        assert op.sha256 == "deadbeef"
        assert op.dry_run is True


# ---------------------------------------------------------------------------
# ManifestEntry
# ---------------------------------------------------------------------------


class TestManifestEntry:
    def _entry(self) -> ManifestEntry:
        return ManifestEntry(
            sha256="deadbeef" * 8,
            size=4096,
            tier="STANDARD",
            modified_at="2024-01-15T12:00:00+00:00",
            etag="etag-123",
            ignored=False,
        )

    def test_to_dict(self):
        entry = self._entry()
        d = entry.to_dict()
        assert d["sha256"] == entry.sha256
        assert d["size"] == 4096
        assert d["tier"] == "STANDARD"
        assert d["modified_at"] == "2024-01-15T12:00:00+00:00"
        assert d["etag"] == "etag-123"
        assert d["ignored"] is False

    def test_from_dict_round_trip(self):
        entry = self._entry()
        d = entry.to_dict()
        restored = ManifestEntry.from_dict(d)
        assert restored.sha256 == entry.sha256
        assert restored.size == entry.size
        assert restored.tier == entry.tier
        assert restored.modified_at == entry.modified_at
        assert restored.etag == entry.etag
        assert restored.ignored == entry.ignored

    def test_from_dict_defaults(self):
        data = {
            "sha256": "abc",
            "size": 10,
            "modified_at": "2024-01-01T00:00:00",
            "etag": "etag",
        }
        entry = ManifestEntry.from_dict(data)
        assert entry.tier == "STANDARD"
        assert entry.ignored is False

    def test_from_dict_ignored_flag(self):
        data = {
            "sha256": "abc",
            "size": 10,
            "modified_at": "2024-01-01T00:00:00",
            "etag": "etag",
            "tier": "GLACIER",
            "ignored": True,
        }
        entry = ManifestEntry.from_dict(data)
        assert entry.ignored is True
        assert entry.tier == "GLACIER"


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------


class TestSyncResult:
    def test_total_changes_empty(self):
        result = SyncResult()
        assert result.total_changes == 0

    def test_total_changes_counts(self):
        result = SyncResult(
            uploaded=["a", "b"],
            downloaded=["c"],
            deleted=["d", "e", "f"],
            moved=[("old", "new")],
        )
        assert result.total_changes == 2 + 1 + 3 + 1  # 7

    def test_had_errors_false(self):
        result = SyncResult()
        assert result.had_errors is False

    def test_had_errors_true(self):
        result = SyncResult(failed=[("path", "error msg")])
        assert result.had_errors is True

    def test_summary_lines_all_empty(self):
        result = SyncResult()
        lines = result.summary_lines()
        assert len(lines) == 1
        assert "up to date" in lines[0].lower()

    def test_summary_lines_with_uploads(self):
        result = SyncResult(uploaded=["a.txt", "b.txt"])
        lines = result.summary_lines()
        assert any("Uploaded" in l and "2" in l for l in lines)

    def test_summary_lines_with_downloads(self):
        result = SyncResult(downloaded=["x.txt"])
        lines = result.summary_lines()
        assert any("Downloaded" in l and "1" in l for l in lines)

    def test_summary_lines_with_deleted(self):
        result = SyncResult(deleted=["d.txt"])
        lines = result.summary_lines()
        assert any("Deleted" in l for l in lines)

    def test_summary_lines_with_moved(self):
        result = SyncResult(moved=[("a.txt", "b.txt")])
        lines = result.summary_lines()
        assert any("Moved" in l for l in lines)

    def test_summary_lines_with_skipped(self):
        result = SyncResult(skipped=["s.txt"])
        lines = result.summary_lines()
        assert any("Skipped" in l for l in lines)

    def test_summary_lines_with_conflicts(self):
        result = SyncResult(conflicts=["c.txt"])
        lines = result.summary_lines()
        assert any("Conflicts" in l for l in lines)

    def test_summary_lines_with_failed(self):
        result = SyncResult(failed=[("f.txt", "some error")])
        lines = result.summary_lines()
        assert any("Failed" in l for l in lines)

    @pytest.mark.parametrize("uploaded,downloaded,deleted,expected_total", [
        ([], [], [], 0),
        (["a"], [], [], 1),
        ([], ["b"], [], 1),
        ([], [], ["c"], 1),
        (["a", "b"], ["c"], ["d"], 4),
    ])
    def test_total_changes_parametrized(self, uploaded, downloaded, deleted, expected_total):
        result = SyncResult(uploaded=uploaded, downloaded=downloaded, deleted=deleted)
        assert result.total_changes == expected_total
