"""Tests for sahara.state_db."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from sahara.models import FileRecord
from sahara.state_db import StateDB


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

NOW = datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_record(
    relative_path: str = "docs/file.txt",
    sha256: str = "abc123",
    size: int = 1024,
    tier: str = "STANDARD",
    etag: str = "etag-abc",
    is_deleted: bool = False,
    restore_job_id: str | None = None,
    restore_expires_at: datetime.datetime | None = None,
    archived_at: datetime.datetime | None = None,
) -> FileRecord:
    return FileRecord(
        relative_path=relative_path,
        sha256_checksum=sha256,
        size_bytes=size,
        tier=tier,
        s3_etag=etag,
        last_sync_at=NOW,
        local_modified_at=NOW,
        remote_modified_at=NOW,
        archived_at=archived_at,
        restore_job_id=restore_job_id,
        restore_expires_at=restore_expires_at,
        is_deleted=is_deleted,
    )


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestStateDBLifecycle:
    def test_creates_schema_on_connect(self, tmp_path: Path):
        db = StateDB(tmp_path / "test.db")
        db.connect()
        # Verify tables exist
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row[0] for row in tables}
        assert "files" in table_names
        assert "history" in table_names
        assert "pending_multipart" in table_names
        assert "config_kv" in table_names
        db.close()

    def test_context_manager(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with StateDB(db_path) as db:
            assert db._conn is not None
        assert db._conn is None

    def test_raises_runtime_error_when_not_connected(self, tmp_path: Path):
        db = StateDB(tmp_path / "test.db")
        with pytest.raises(RuntimeError, match="not connected"):
            _ = db.conn

    def test_double_close_is_safe(self, tmp_path: Path):
        db = StateDB(tmp_path / "test.db")
        db.connect()
        db.close()
        db.close()  # Should not raise

    def test_default_path_used_when_none(self):
        db = StateDB(None)
        from sahara.state_db import DB_PATH
        assert db._path == DB_PATH


# ---------------------------------------------------------------------------
# files table
# ---------------------------------------------------------------------------


class TestFilesTable:
    def test_upsert_and_get_file(self, in_memory_db: StateDB):
        rec = _make_record()
        in_memory_db.upsert_file(rec)
        fetched = in_memory_db.get_file("docs/file.txt")
        assert fetched is not None
        assert fetched.relative_path == "docs/file.txt"
        assert fetched.sha256_checksum == "abc123"
        assert fetched.size_bytes == 1024
        assert fetched.tier == "STANDARD"

    def test_get_file_returns_none_for_unknown(self, in_memory_db: StateDB):
        result = in_memory_db.get_file("nonexistent.txt")
        assert result is None

    def test_upsert_updates_existing_record(self, in_memory_db: StateDB):
        rec = _make_record(sha256="original")
        in_memory_db.upsert_file(rec)

        updated = _make_record(sha256="updated-sha")
        in_memory_db.upsert_file(updated)

        fetched = in_memory_db.get_file("docs/file.txt")
        assert fetched.sha256_checksum == "updated-sha"

    def test_delete_file_removes_record(self, in_memory_db: StateDB):
        rec = _make_record()
        in_memory_db.upsert_file(rec)
        in_memory_db.delete_file("docs/file.txt")
        assert in_memory_db.get_file("docs/file.txt") is None

    def test_mark_deleted_soft_delete(self, in_memory_db: StateDB):
        rec = _make_record()
        in_memory_db.upsert_file(rec)
        in_memory_db.mark_deleted("docs/file.txt")

        fetched = in_memory_db.get_file("docs/file.txt")
        assert fetched is not None
        assert fetched.is_deleted is True

    def test_list_files_excludes_deleted_by_default(self, in_memory_db: StateDB):
        active = _make_record(relative_path="active.txt")
        deleted = _make_record(relative_path="deleted.txt", is_deleted=True)
        in_memory_db.upsert_file(active)
        in_memory_db.upsert_file(deleted)

        files = in_memory_db.list_files()
        paths = [f.relative_path for f in files]
        assert "active.txt" in paths
        assert "deleted.txt" not in paths

    def test_list_files_with_include_deleted(self, in_memory_db: StateDB):
        active = _make_record(relative_path="active.txt")
        deleted = _make_record(relative_path="deleted.txt", is_deleted=True)
        in_memory_db.upsert_file(active)
        in_memory_db.upsert_file(deleted)

        files = in_memory_db.list_files(include_deleted=True)
        paths = [f.relative_path for f in files]
        assert "active.txt" in paths
        assert "deleted.txt" in paths

    def test_list_files_by_tier(self, in_memory_db: StateDB):
        standard = _make_record(relative_path="standard.txt", tier="STANDARD")
        glacier = _make_record(relative_path="glacier.txt", tier="GLACIER")
        in_memory_db.upsert_file(standard)
        in_memory_db.upsert_file(glacier)

        std_files = in_memory_db.list_files_by_tier("STANDARD")
        glac_files = in_memory_db.list_files_by_tier("GLACIER")

        assert any(f.relative_path == "standard.txt" for f in std_files)
        assert not any(f.relative_path == "glacier.txt" for f in std_files)
        assert any(f.relative_path == "glacier.txt" for f in glac_files)

    def test_list_files_by_tier_excludes_deleted(self, in_memory_db: StateDB):
        deleted = _make_record(relative_path="del.txt", tier="STANDARD", is_deleted=True)
        in_memory_db.upsert_file(deleted)
        files = in_memory_db.list_files_by_tier("STANDARD")
        assert not any(f.relative_path == "del.txt" for f in files)

    def test_list_files_by_sha256(self, in_memory_db: StateDB):
        sha = "sha256-unique"
        rec = _make_record(sha256=sha)
        in_memory_db.upsert_file(rec)

        results = in_memory_db.list_files_by_sha256(sha)
        assert len(results) == 1
        assert results[0].sha256_checksum == sha

    def test_list_files_by_sha256_returns_empty_for_unknown(self, in_memory_db: StateDB):
        results = in_memory_db.list_files_by_sha256("notexist")
        assert results == []

    def test_get_total_size_by_tier(self, in_memory_db: StateDB):
        in_memory_db.upsert_file(_make_record(relative_path="a.txt", tier="STANDARD", size=100))
        in_memory_db.upsert_file(_make_record(relative_path="b.txt", tier="STANDARD", size=200))
        in_memory_db.upsert_file(_make_record(relative_path="c.txt", tier="GLACIER", size=50))

        sizes = in_memory_db.get_total_size_by_tier()
        assert sizes["STANDARD"] == 300
        assert sizes["GLACIER"] == 50

    def test_get_total_size_excludes_deleted(self, in_memory_db: StateDB):
        in_memory_db.upsert_file(_make_record(relative_path="a.txt", size=100))
        in_memory_db.upsert_file(_make_record(relative_path="b.txt", size=999, is_deleted=True))

        sizes = in_memory_db.get_total_size_by_tier()
        assert sizes.get("STANDARD", 0) == 100


# ---------------------------------------------------------------------------
# history table
# ---------------------------------------------------------------------------


class TestHistoryTable:
    def test_add_and_get_history(self, in_memory_db: StateDB):
        in_memory_db.add_history("docs/file.txt", "upload", sha256="abc", size_bytes=512)
        history = in_memory_db.get_history()
        assert len(history) >= 1
        entry = history[0]
        assert entry["relative_path"] == "docs/file.txt"
        assert entry["operation"] == "upload"
        assert entry["sha256"] == "abc"
        assert entry["size_bytes"] == 512

    def test_get_history_filtered_by_path(self, in_memory_db: StateDB):
        in_memory_db.add_history("file_a.txt", "upload")
        in_memory_db.add_history("file_b.txt", "download")

        history_a = in_memory_db.get_history(relative_path="file_a.txt")
        assert all(e["relative_path"] == "file_a.txt" for e in history_a)
        assert not any(e["relative_path"] == "file_b.txt" for e in history_a)

    def test_get_history_limit(self, in_memory_db: StateDB):
        for i in range(10):
            in_memory_db.add_history(f"file_{i}.txt", "upload")

        history = in_memory_db.get_history(limit=5)
        assert len(history) <= 5

    def test_add_history_optional_fields(self, in_memory_db: StateDB):
        in_memory_db.add_history("f.txt", "move", details="from:old.txt")
        history = in_memory_db.get_history()
        assert history[0]["details"] == "from:old.txt"

    def test_history_without_path_filter(self, in_memory_db: StateDB):
        in_memory_db.add_history("a.txt", "upload")
        in_memory_db.add_history("b.txt", "download")
        history = in_memory_db.get_history()
        paths = [e["relative_path"] for e in history]
        assert "a.txt" in paths
        assert "b.txt" in paths


# ---------------------------------------------------------------------------
# pending_multipart table
# ---------------------------------------------------------------------------


class TestPendingMultipart:
    def test_upsert_and_get(self, in_memory_db: StateDB):
        in_memory_db.upsert_pending_multipart(
            relative_path="big.zip",
            s3_key="uploads/big.zip",
            upload_id="uid-123",
            file_sha256="sha256-big",
            size_bytes=100 * 1024 * 1024,
        )
        result = in_memory_db.get_pending_multipart("big.zip")
        assert result is not None
        assert result["upload_id"] == "uid-123"
        assert result["file_sha256"] == "sha256-big"

    def test_get_returns_none_for_unknown(self, in_memory_db: StateDB):
        result = in_memory_db.get_pending_multipart("nonexistent.zip")
        assert result is None

    def test_delete_pending_multipart(self, in_memory_db: StateDB):
        in_memory_db.upsert_pending_multipart(
            relative_path="big.zip",
            s3_key="uploads/big.zip",
            upload_id="uid-del",
            file_sha256="sha",
            size_bytes=0,
        )
        in_memory_db.delete_pending_multipart("big.zip")
        assert in_memory_db.get_pending_multipart("big.zip") is None

    def test_update_pending_multipart_parts(self, in_memory_db: StateDB):
        in_memory_db.upsert_pending_multipart(
            relative_path="big.zip",
            s3_key="big.zip",
            upload_id="uid",
            file_sha256="sha",
            size_bytes=0,
            parts_json="[]",
        )
        new_parts = '[{"PartNumber": 1, "ETag": "etag1"}]'
        in_memory_db.update_pending_multipart_parts("big.zip", new_parts)
        result = in_memory_db.get_pending_multipart("big.zip")
        assert result["parts_json"] == new_parts

    def test_get_pending_multiparts_returns_all(self, in_memory_db: StateDB):
        in_memory_db.upsert_pending_multipart("a.zip", "a.zip", "uid1", "sha1", 0)
        in_memory_db.upsert_pending_multipart("b.zip", "b.zip", "uid2", "sha2", 0)
        results = in_memory_db.get_pending_multiparts()
        assert len(results) == 2

    def test_upsert_updates_existing(self, in_memory_db: StateDB):
        in_memory_db.upsert_pending_multipart("big.zip", "big.zip", "uid-old", "sha", 0)
        in_memory_db.upsert_pending_multipart("big.zip", "big.zip", "uid-new", "sha", 0)
        result = in_memory_db.get_pending_multipart("big.zip")
        assert result["upload_id"] == "uid-new"


# ---------------------------------------------------------------------------
# config_kv table
# ---------------------------------------------------------------------------


class TestConfigKV:
    def test_set_and_get(self, in_memory_db: StateDB):
        in_memory_db.set_config_value("my_key", "my_value")
        val = in_memory_db.get_config_value("my_key")
        assert val == "my_value"

    def test_get_returns_none_for_missing(self, in_memory_db: StateDB):
        val = in_memory_db.get_config_value("nonexistent")
        assert val is None

    def test_delete_config_value(self, in_memory_db: StateDB):
        in_memory_db.set_config_value("key_to_delete", "value")
        in_memory_db.delete_config_value("key_to_delete")
        assert in_memory_db.get_config_value("key_to_delete") is None

    def test_set_config_value_updates_existing(self, in_memory_db: StateDB):
        in_memory_db.set_config_value("key", "old")
        in_memory_db.set_config_value("key", "new")
        assert in_memory_db.get_config_value("key") == "new"

    def test_delete_nonexistent_does_not_raise(self, in_memory_db: StateDB):
        # Should not raise even if key doesn't exist
        in_memory_db.delete_config_value("ghost_key")


# ---------------------------------------------------------------------------
# Restore-tracking helpers
# ---------------------------------------------------------------------------


class TestRestoreHelpers:
    def test_list_pending_restores_empty(self, in_memory_db: StateDB):
        result = in_memory_db.list_pending_restores()
        assert result == []

    def test_list_pending_restores_finds_records(self, in_memory_db: StateDB):
        rec = _make_record(
            relative_path="archived.zip",
            tier="GLACIER",
            restore_job_id="job-abc",
        )
        in_memory_db.upsert_file(rec)
        results = in_memory_db.list_pending_restores()
        assert len(results) == 1
        assert results[0].relative_path == "archived.zip"

    def test_list_pending_restores_excludes_deleted(self, in_memory_db: StateDB):
        rec = _make_record(
            relative_path="deleted.zip",
            tier="GLACIER",
            restore_job_id="job-xyz",
            is_deleted=True,
        )
        in_memory_db.upsert_file(rec)
        results = in_memory_db.list_pending_restores()
        assert not any(r.relative_path == "deleted.zip" for r in results)

    def test_list_expiring_restores(self, in_memory_db: StateDB):
        # A restore expiring in 1 hour should appear in within_hours=48
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        rec = _make_record(
            relative_path="expiring.zip",
            tier="HOT_TEMP",
            restore_expires_at=soon,
        )
        in_memory_db.upsert_file(rec)
        results = in_memory_db.list_expiring_restores(within_hours=48)
        assert any(r.relative_path == "expiring.zip" for r in results)

    def test_list_expiring_restores_excludes_far_future(self, in_memory_db: StateDB):
        far_future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        rec = _make_record(
            relative_path="farfuture.zip",
            tier="HOT_TEMP",
            restore_expires_at=far_future,
        )
        in_memory_db.upsert_file(rec)
        results = in_memory_db.list_expiring_restores(within_hours=1)
        assert not any(r.relative_path == "farfuture.zip" for r in results)

    def test_list_expiring_restores_excludes_non_hot_temp(self, in_memory_db: StateDB):
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        rec = _make_record(
            relative_path="not_hot.zip",
            tier="GLACIER",
            restore_expires_at=soon,
        )
        in_memory_db.upsert_file(rec)
        results = in_memory_db.list_expiring_restores(within_hours=48)
        assert not any(r.relative_path == "not_hot.zip" for r in results)


# ---------------------------------------------------------------------------
# Transaction context manager
# ---------------------------------------------------------------------------


class TestTransaction:
    def test_transaction_commit_on_success(self, in_memory_db: StateDB):
        rec = _make_record()
        with in_memory_db.transaction():
            in_memory_db.conn.execute(
                "INSERT INTO files "
                "(relative_path, sha256_checksum, size_bytes, tier, s3_etag, "
                "last_sync_at, local_modified_at, remote_modified_at, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "tx_test.txt", "sha", 0, "STANDARD", "etag",
                    NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), 0,
                ),
            )
        assert in_memory_db.get_file("tx_test.txt") is not None

    def test_transaction_rollback_on_exception(self, in_memory_db: StateDB):
        try:
            with in_memory_db.transaction():
                in_memory_db.conn.execute(
                    "INSERT INTO files "
                    "(relative_path, sha256_checksum, size_bytes, tier, s3_etag, "
                    "last_sync_at, local_modified_at, remote_modified_at, is_deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "rollback_test.txt", "sha", 0, "STANDARD", "etag",
                        NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), 0,
                    ),
                )
                raise ValueError("Simulated error")
        except ValueError:
            pass
        # Row should have been rolled back
        assert in_memory_db.get_file("rollback_test.txt") is None
