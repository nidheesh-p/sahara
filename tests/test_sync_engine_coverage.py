"""Targeted tests to improve sync_engine.py coverage on specific code paths."""
from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from sahara.config import SaharaConfig
from sahara.ignore_rules import IgnoreRules
from sahara.models import FileRecord, ManifestEntry, SyncResult
from sahara.s3_client import ManifestConflictError, S3Client, S3ClientError
from sahara.state_db import StateDB
from sahara.sync_engine import DiffResult, SyncEngine

BUCKET = "sync-cov-bucket"
REGION = "us-east-1"
NOW = datetime.datetime.now(datetime.UTC)


def _make_config(tmp_path: Path, **kwargs) -> SaharaConfig:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)
    return SaharaConfig(
        sync_folder=str(sync_folder),
        bucket=BUCKET,
        region=REGION,
        prefix="",
        max_workers=2,
        **kwargs,
    )


def _make_engine(tmp_path: Path, mock_db=None, mock_s3=None, **cfg_kwargs) -> SyncEngine:
    cfg = _make_config(tmp_path, **cfg_kwargs)
    if mock_db is None:
        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_db.list_files_by_tier.return_value = []
        mock_db.get_file.return_value = None
    if mock_s3 is None:
        mock_s3 = MagicMock()
        mock_s3.get_manifest.return_value = ({}, "etag")
        mock_s3.list_multipart_uploads.return_value = []
    ignore = IgnoreRules(cfg.get_sync_folder_path())
    return SyncEngine(cfg, mock_db, mock_s3, ignore)


def _make_file_record(path="file.txt", sha="abc123", tier="STANDARD"):
    return FileRecord(
        relative_path=path,
        sha256_checksum=sha,
        size_bytes=100,
        tier=tier,
        s3_etag="etag",
        last_sync_at=NOW,
        local_modified_at=NOW,
        remote_modified_at=NOW,
    )


# ---------------------------------------------------------------------------
# _execute_delete_remote / _execute_delete_local
# ---------------------------------------------------------------------------


class TestExecuteDeleteOps:
    def test_execute_delete_remote_success(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.delete_object.return_value = None
        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        result = engine._execute_delete_remote("file.txt")
        assert result is True
        mock_s3.delete_object.assert_called_once()

    def test_execute_delete_remote_fails(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.delete_object.side_effect = RuntimeError("S3 error")
        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        result = engine._execute_delete_remote("file.txt")
        assert result is False

    def test_execute_delete_local_success(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Create a file to delete
        sync_folder = engine._sync_folder
        local_file = sync_folder / "file.txt"
        local_file.write_text("hello")

        result = engine._execute_delete_local("file.txt")
        assert result is True
        assert not local_file.exists()

    def test_execute_delete_local_missing_file_ok(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # File doesn't exist — missing_ok=True should not fail
        result = engine._execute_delete_local("nonexistent.txt")
        assert result is True

    def test_execute_delete_local_fails_on_error(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            result = engine._execute_delete_local("file.txt")
            assert result is False


# ---------------------------------------------------------------------------
# _execute_move
# ---------------------------------------------------------------------------


class TestExecuteMove:
    def test_execute_move_success(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.copy_object.return_value = "newtag"
        mock_s3.delete_object.return_value = None

        mock_db = MagicMock()
        mock_db.get_file.return_value = _make_file_record("old.txt")

        engine = _make_engine(tmp_path, mock_db=mock_db, mock_s3=mock_s3)

        # Create the new file at new path location
        new_file = engine._sync_folder / "new.txt"
        new_file.write_text("content")

        record = engine._execute_move("old.txt", "new.txt")
        assert record is not None
        assert record.relative_path == "new.txt"
        assert record.s3_etag == "newtag"

    def test_execute_move_no_db_record(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.copy_object.return_value = "newtag"
        mock_s3.delete_object.return_value = None

        mock_db = MagicMock()
        mock_db.get_file.return_value = None  # No existing record

        engine = _make_engine(tmp_path, mock_db=mock_db, mock_s3=mock_s3)

        # Create the new file
        new_file = engine._sync_folder / "new.txt"
        new_file.write_bytes(b"content data")

        record = engine._execute_move("old.txt", "new.txt")
        assert record is not None
        assert record.relative_path == "new.txt"

    def test_execute_move_s3_fails(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.copy_object.side_effect = RuntimeError("S3 error")

        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        result = engine._execute_move("old.txt", "new.txt")
        assert result is None

    def test_execute_move_new_file_not_exists(self, tmp_path: Path):
        """When new file doesn't exist locally, use size from old record."""
        mock_s3 = MagicMock()
        mock_s3.copy_object.return_value = "newtag"
        mock_s3.delete_object.return_value = None

        existing = _make_file_record("old.txt")
        existing.size_bytes = 999
        mock_db = MagicMock()
        mock_db.get_file.return_value = existing

        engine = _make_engine(tmp_path, mock_db=mock_db, mock_s3=mock_s3)
        # Don't create new.txt locally

        record = engine._execute_move("old.txt", "new.txt")
        assert record is not None
        assert record.size_bytes == 999


# ---------------------------------------------------------------------------
# _execute_download
# ---------------------------------------------------------------------------


class TestExecuteDownload:
    def test_execute_download_sha_mismatch_logs_warning(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.download_file.return_value = "downloaded_sha"

        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        # Create parent dir
        local = engine._sync_folder / "file.txt"
        local.parent.mkdir(parents=True, exist_ok=True)

        entry = ManifestEntry(
            sha256="expected_sha",  # Different from downloaded_sha
            size=100,
            tier="STANDARD",
            modified_at=NOW.isoformat(),
            etag="etag",
        )

        with patch("sahara.sync_engine.logger") as mock_logger:
            record = engine._execute_download("file.txt", entry)
            assert record is not None  # Should still return record
            # Warning about mismatch should have been logged
            mock_logger.warning.assert_called()

    def test_execute_download_invalid_mtime_falls_back(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.download_file.return_value = "sha256"

        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        entry = ManifestEntry(
            sha256="sha256",
            size=100,
            tier="STANDARD",
            modified_at="not-a-valid-iso-date",  # Invalid date
            etag="etag",
        )

        record = engine._execute_download("file.txt", entry)
        assert record is not None
        # Should fall back to now for the mtime

    def test_execute_download_fails_returns_none(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = RuntimeError("Download failed")

        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        entry = ManifestEntry(
            sha256="sha256",
            size=100,
            tier="STANDARD",
            modified_at=NOW.isoformat(),
            etag="etag",
        )

        result = engine._execute_download("file.txt", entry)
        assert result is None

    def test_execute_download_encryption_enabled_no_passphrase(self, tmp_path: Path):
        mock_s3 = MagicMock()
        engine = _make_engine(tmp_path, mock_s3=mock_s3, encryption_enabled=True)

        entry = ManifestEntry(
            sha256="sha256",
            size=100,
            tier="STANDARD",
            modified_at=NOW.isoformat(),
            etag="etag",
        )

        with patch("sahara.sync_engine.get_passphrase", return_value=None):
            result = engine._execute_download("file.txt", entry)
            assert result is None  # Should fail gracefully


# ---------------------------------------------------------------------------
# _write_manifest_with_retry
# ---------------------------------------------------------------------------


class TestWriteManifestWithRetry:
    def test_write_manifest_retries_on_conflict(self, tmp_path: Path):
        mock_s3 = MagicMock()
        call_count = [0]

        def put_manifest_side_effect(manifest, if_match_etag=None, key=None):
            call_count[0] += 1
            if call_count[0] < 2:
                raise ManifestConflictError("Conflict")

        mock_s3.put_manifest.side_effect = put_manifest_side_effect
        mock_s3.get_manifest.return_value = ({"existing": {"sha256": "abc"}}, "new-etag")

        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        # Should succeed on 2nd attempt
        engine._write_manifest_with_retry({"key": "val"}, "etag")
        assert call_count[0] == 2

    def test_write_manifest_raises_after_max_retries(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.put_manifest.side_effect = ManifestConflictError("Conflict")
        mock_s3.get_manifest.return_value = ({}, "etag")

        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        with pytest.raises(S3ClientError, match="Failed to write manifest"):
            engine._write_manifest_with_retry({"key": "val"}, "etag")

        # Should have retried 3 times
        assert mock_s3.put_manifest.call_count == 3

    def test_write_manifest_merges_on_conflict_retry(self, tmp_path: Path):
        mock_s3 = MagicMock()
        call_count = [0]

        def put_manifest_side_effect(manifest, if_match_etag=None, key=None):
            call_count[0] += 1
            if call_count[0] < 2:
                raise ManifestConflictError("Conflict")
            # Record what manifest was used on success
            put_manifest_side_effect.last_manifest = manifest

        put_manifest_side_effect.last_manifest = None

        mock_s3.put_manifest.side_effect = put_manifest_side_effect
        # Remote manifest has "remote_key", our manifest has "local_key"
        mock_s3.get_manifest.return_value = (
            {"remote_key": {"sha256": "remote-sha", "size": 0, "tier": "STANDARD",
                            "modified_at": NOW.isoformat(), "etag": "e"}},
            "new-etag"
        )

        engine = _make_engine(tmp_path, mock_s3=mock_s3)

        local_manifest = {"local_key": {"sha256": "local-sha", "size": 0, "tier": "STANDARD",
                                         "modified_at": NOW.isoformat(), "etag": "e"}}
        engine._write_manifest_with_retry(local_manifest, "etag")

        # Merged manifest should have both keys
        final_manifest = put_manifest_side_effect.last_manifest
        assert "remote_key" in final_manifest
        assert "local_key" in final_manifest


# ---------------------------------------------------------------------------
# sync with execute operations (full integration with moto)
# ---------------------------------------------------------------------------


class TestSyncWithMoto:
    def test_sync_with_local_new_file(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)

            db_path = tmp_path / "state.db"
            db = StateDB(db_path)
            db.connect()

            s3 = S3Client(cfg)
            ignore = IgnoreRules(cfg.get_sync_folder_path())
            engine = SyncEngine(cfg, db, s3, ignore)

            # Create a local file
            local_file = cfg.get_sync_folder_path() / "hello.txt"
            local_file.write_bytes(b"hello world")

            result = engine.sync()
            assert "hello.txt" in result.uploaded
            db.close()

    def test_sync_with_remote_new_file(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)

            # Put a file in S3 and create a manifest
            raw.put_object(Bucket=BUCKET, Key="remote.txt", Body=b"remote content")

            # Create manifest pointing to the file
            import json
            manifest = {
                "remote.txt": {
                    "sha256": "a" * 64,
                    "size": 14,
                    "tier": "STANDARD",
                    "modified_at": NOW.isoformat(),
                    "etag": "\"remotetag\"",
                }
            }
            raw.put_object(
                Bucket=BUCKET,
                Key=".sahara/manifest.json",
                Body=json.dumps(manifest).encode(),
                ContentType="application/json",
            )

            db_path = tmp_path / "state.db"
            db = StateDB(db_path)
            db.connect()

            s3 = S3Client(cfg)
            ignore = IgnoreRules(cfg.get_sync_folder_path())
            engine = SyncEngine(cfg, db, s3, ignore)

            result = engine.sync(pull_only=True)
            assert "remote.txt" in result.downloaded
            db.close()

    def test_sync_with_delete_operations(self, tmp_path: Path):
        """Test that delete_remote and delete_local ops work."""
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config(
                tmp_path,
                delete_remote_on_local_delete=True,
                delete_local_on_remote_delete=True,
            )

            db_path = tmp_path / "state.db"
            db = StateDB(db_path)
            db.connect()

            # Add a file to DB as if it was previously synced
            record = _make_file_record("deleted_locally.txt")
            db.upsert_file(record)

            # Upload to S3
            raw.put_object(Bucket=BUCKET, Key="deleted_locally.txt", Body=b"content")

            # File does NOT exist locally (simulating local delete)
            # So the engine should see it as local_deleted

            import json
            manifest = {
                "deleted_locally.txt": {
                    "sha256": "abc123",
                    "size": 7,
                    "tier": "STANDARD",
                    "modified_at": NOW.isoformat(),
                    "etag": "\"etag\"",
                }
            }
            raw.put_object(
                Bucket=BUCKET,
                Key=".sahara/manifest.json",
                Body=json.dumps(manifest).encode(),
                ContentType="application/json",
            )

            s3 = S3Client(cfg)
            ignore = IgnoreRules(cfg.get_sync_folder_path())
            engine = SyncEngine(cfg, db, s3, ignore)

            result = engine.sync(push_only=True)
            # Local delete should have triggered remote delete
            assert "deleted_locally.txt" in result.deleted
            db.close()

    def test_sync_with_move_detection(self, tmp_path: Path):
        """Test rename detection in sync."""
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)

            content = b"unique content for rename detection"
            import hashlib
            sha = hashlib.sha256(content).hexdigest()

            cfg = _make_config(tmp_path)
            db_path = tmp_path / "state.db"
            db = StateDB(db_path)
            db.connect()

            # Add old file to DB as if previously synced
            record = _make_file_record("old_name.txt", sha=sha)
            db.upsert_file(record)

            # Upload old file to S3
            raw.put_object(Bucket=BUCKET, Key="old_name.txt", Body=content)

            # Create manifest with old file
            import json
            manifest = {
                "old_name.txt": {
                    "sha256": sha,
                    "size": len(content),
                    "tier": "STANDARD",
                    "modified_at": NOW.isoformat(),
                    "etag": "\"etag\"",
                }
            }
            raw.put_object(
                Bucket=BUCKET,
                Key=".sahara/manifest.json",
                Body=json.dumps(manifest).encode(),
                ContentType="application/json",
            )

            # Create new file locally with same content (rename simulation)
            new_file = cfg.get_sync_folder_path() / "new_name.txt"
            new_file.write_bytes(content)

            s3 = S3Client(cfg)
            ignore = IgnoreRules(cfg.get_sync_folder_path())
            engine = SyncEngine(cfg, db, s3, ignore)

            result = engine.sync(push_only=True)
            # Should detect the rename as a move
            assert ("old_name.txt", "new_name.txt") in result.moved
            db.close()

    def test_sync_verify_flag(self, tmp_path: Path):
        """Test that verify flag causes HEAD checks on uploaded files."""
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)

            db_path = tmp_path / "state.db"
            db = StateDB(db_path)
            db.connect()

            s3 = S3Client(cfg)
            ignore = IgnoreRules(cfg.get_sync_folder_path())
            engine = SyncEngine(cfg, db, s3, ignore)

            # Create a local file
            local_file = cfg.get_sync_folder_path() / "verify_me.txt"
            local_file.write_bytes(b"verify test data")

            result = engine.sync(verify=True)
            assert "verify_me.txt" in result.uploaded
            db.close()

    def test_sync_lock_timeout_raises(self, tmp_path: Path):
        """Test that a lock timeout raises S3ClientError."""
        cfg = _make_config(tmp_path)
        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_s3 = MagicMock()
        mock_s3.get_manifest.return_value = ({}, "etag")
        ignore = IgnoreRules(cfg.get_sync_folder_path())
        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)

        import filelock
        # Patch FileLock so acquire raises a Timeout
        mock_lock = MagicMock()
        mock_lock.acquire.side_effect = filelock.Timeout("lock")
        with patch("filelock.FileLock", return_value=mock_lock):
            with pytest.raises(S3ClientError, match="Another sync"):
                engine.sync()


# ---------------------------------------------------------------------------
# _build_manifest_from_db
# ---------------------------------------------------------------------------


class TestBuildManifestFromDb:
    def test_build_manifest_returns_all_files(self, tmp_path: Path):
        mock_db = MagicMock()
        records = [
            _make_file_record("file1.txt", sha="sha1"),
            _make_file_record("file2.txt", sha="sha2"),
        ]
        mock_db.list_files.return_value = records

        engine = _make_engine(tmp_path, mock_db=mock_db)
        manifest = engine._build_manifest_from_db()

        assert "file1.txt" in manifest
        assert "file2.txt" in manifest
        assert manifest["file1.txt"]["sha256"] == "sha1"

    def test_build_manifest_empty_db(self, tmp_path: Path):
        mock_db = MagicMock()
        mock_db.list_files.return_value = []

        engine = _make_engine(tmp_path, mock_db=mock_db)
        manifest = engine._build_manifest_from_db()

        assert manifest == {}


# ---------------------------------------------------------------------------
# _resolve_conflicts — all strategies
# ---------------------------------------------------------------------------


class TestResolveConflictsAllStrategies:
    def test_resolve_backup_strategy_creates_backup(self, tmp_path: Path):
        cfg = _make_config(tmp_path, conflict_strategy="backup")
        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_s3 = MagicMock()
        mock_s3.get_manifest.return_value = ({}, "etag")
        ignore = IgnoreRules(cfg.get_sync_folder_path())
        engine = SyncEngine(cfg, mock_db, mock_s3, ignore)

        # Create conflict file
        conflict_file = engine._sync_folder / "conflict.txt"
        conflict_file.write_bytes(b"local content")

        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()

        uploads, downloads, skips = engine._resolve_conflicts(diff, "backup", result)
        assert "conflict.txt" in downloads
        # backup file should be created
        backup_files = list(engine._sync_folder.glob("conflict.txt.conflict-*"))
        assert len(backup_files) == 1

    def test_resolve_local_strategy(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()

        uploads, downloads, skips = engine._resolve_conflicts(diff, "local", result)
        assert "conflict.txt" in uploads
        assert "conflict.txt" not in downloads

    def test_resolve_remote_strategy(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()

        uploads, downloads, skips = engine._resolve_conflicts(diff, "remote", result)
        assert "conflict.txt" in downloads
        assert "conflict.txt" not in uploads

    def test_resolve_ask_strategy_skips(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()

        uploads, downloads, skips = engine._resolve_conflicts(diff, "ask", result)
        assert "conflict.txt" in skips
        assert "conflict.txt" not in uploads
        assert "conflict.txt" not in downloads

    def test_resolve_backup_os_error_skips(self, tmp_path: Path):
        engine = _make_engine(tmp_path)

        # Create the conflict file
        conflict_file = engine._sync_folder / "conflict.txt"
        conflict_file.write_bytes(b"local")

        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()

        # Make shutil.copy2 fail
        with patch("shutil.copy2", side_effect=OSError("permission denied")):
            uploads, downloads, skips = engine._resolve_conflicts(diff, "backup", result)
            assert "conflict.txt" in skips


# ---------------------------------------------------------------------------
# get_status with bootstrap
# ---------------------------------------------------------------------------


class TestGetStatusBootstrap:
    def test_get_status_with_no_manifest_bootstraps(self, tmp_path: Path):
        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_s3 = MagicMock()
        mock_s3.get_manifest.return_value = (None, None)  # No manifest
        mock_s3.list_all_objects.return_value = []

        engine = _make_engine(tmp_path, mock_db=mock_db, mock_s3=mock_s3)
        diff = engine.get_status()

        assert isinstance(diff, DiffResult)
        mock_s3.list_all_objects.assert_called()


# ---------------------------------------------------------------------------
# download_restored
# ---------------------------------------------------------------------------


class TestDownloadRestoredCoverage:
    def test_download_restored_when_not_ready(self, tmp_path: Path):
        engine = _make_engine(tmp_path)

        with patch.object(engine, "check_restore_status", return_value={
            "ready": False,
            "tier": "GLACIER",
            "restore_header": 'ongoing-request="true"',
            "expires_at": None,
        }):
            result = engine.download_restored("file.zip")
            assert result is None

    def test_download_restored_uses_manifest_entry(self, tmp_path: Path):
        mock_s3 = MagicMock()
        mock_s3.get_manifest.return_value = (
            {
                "file.zip": {
                    "sha256": "realsha",
                    "size": 100,
                    "tier": "GLACIER",
                    "modified_at": NOW.isoformat(),
                    "etag": "etag",
                }
            },
            "etag",
        )
        mock_s3.download_file.return_value = "realsha"

        mock_db = MagicMock()
        mock_db.list_files.return_value = []
        mock_db.get_file.return_value = None

        engine = _make_engine(tmp_path, mock_db=mock_db, mock_s3=mock_s3)

        with patch.object(engine, "check_restore_status", return_value={
            "ready": True,
            "tier": "GLACIER",
            "restore_header": 'ongoing-request="false"',
            "expires_at": None,
        }):
            result = engine.download_restored("file.zip")
            # download_restored returns path on success, not sha256
            assert result == "file.zip"
            mock_db.upsert_file.assert_called_once()
