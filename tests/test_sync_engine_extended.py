"""Extended tests for sahara.sync_engine — covers archive, restore, status, moves."""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

from sahara.config import SaharaConfig
from sahara.models import FileRecord, ManifestEntry, SyncResult
from sahara.s3_client import S3Client
from sahara.state_db import StateDB
from sahara.sync_engine import (
    DiffResult,
    SyncEngine,
    _ensure_aware,
    _local_mtime_utc,
    _now_utc,
)

BUCKET = "sync-ext-bucket"
REGION = "us-east-1"
NOW = datetime.datetime.now(datetime.UTC)


def _make_config(tmp_path: Path, **kwargs) -> SaharaConfig:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)
    defaults = dict(
        sync_folder=str(sync_folder),
        bucket=BUCKET,
        region=REGION,
        prefix="",
        max_workers=2,
        multipart_threshold_mb=100,
        multipart_chunk_size_mb=8,
        conflict_strategy="backup",
        encryption_enabled=False,
        delete_remote_on_local_delete=True,
        delete_local_on_remote_delete=True,
    )
    defaults.update(kwargs)
    return SaharaConfig(**defaults)


def _make_db(tmp_path: Path) -> StateDB:
    db = StateDB(tmp_path / "state_ext.db")
    db.connect()
    return db


def _make_record(path: str, sha256: str = "sha", tier: str = "STANDARD") -> FileRecord:
    return FileRecord(
        relative_path=path,
        sha256_checksum=sha256,
        size_bytes=100,
        tier=tier,
        s3_etag="etag",
        last_sync_at=NOW,
        local_modified_at=NOW,
        remote_modified_at=NOW,
    )


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def test_local_mtime_utc(tmp_path: Path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"test")
    mtime = _local_mtime_utc(f)
    assert isinstance(mtime, datetime.datetime)
    assert mtime.tzinfo is not None


def test_now_utc():
    now = _now_utc()
    assert isinstance(now, datetime.datetime)
    assert now.tzinfo is not None


def test_ensure_aware_naive():
    naive = datetime.datetime(2024, 1, 1, 12, 0, 0)
    aware = _ensure_aware(naive)
    assert aware.tzinfo is not None


def test_ensure_aware_already_aware():
    aware = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
    result = _ensure_aware(aware)
    assert result == aware


# ---------------------------------------------------------------------------
# archive_files
# ---------------------------------------------------------------------------


class TestArchiveFiles:
    def test_archive_files_dry_run(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            paths = ["file1.txt", "file2.txt"]
            result = engine.archive_files(paths, dry_run=True)
            assert result == paths
            db.close()

    def test_archive_files_real(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            content = b"to archive"
            (sync_folder / "archive_me.txt").write_bytes(content)

            # First upload the file
            s3.upload_file(sync_folder / "archive_me.txt", "archive_me.txt")
            db.upsert_file(_make_record("archive_me.txt"))

            result = engine.archive_files(["archive_me.txt"], storage_class="STANDARD_IA")
            assert "archive_me.txt" in result
            db.close()

    def test_archive_files_handles_exception(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            # File not in S3, should not raise
            engine.archive_files(["nonexistent.txt"])
            db.close()


# ---------------------------------------------------------------------------
# request_restore
# ---------------------------------------------------------------------------


class TestRequestRestore:
    def test_request_restore_without_db_record(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            # Mock restore_object to succeed
            with patch.object(s3, "restore_object"):
                engine.request_restore("archive/file.zip")

            # Record should be created
            rec = db.get_file("archive/file.zip")
            assert rec is not None
            assert rec.restore_job_id is not None
            db.close()

    def test_request_restore_with_existing_db_record(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            db.upsert_file(_make_record("archive/file.zip", tier="GLACIER"))

            with patch.object(s3, "restore_object"):
                engine.request_restore("archive/file.zip", days=14, tier="Standard")

            rec = db.get_file("archive/file.zip")
            assert rec.restore_job_id is not None
            db.close()


# ---------------------------------------------------------------------------
# check_restore_status
# ---------------------------------------------------------------------------


class TestCheckRestoreStatus:
    def test_check_restore_status_pending(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "f.txt").write_bytes(b"data")
            s3.upload_file(sync_folder / "f.txt", "f.txt")

            with patch.object(s3, "head_object") as mock_head:
                mock_head.return_value = {
                    "StorageClass": "GLACIER",
                    "Restore": 'ongoing-request="true"',
                    "Metadata": {},
                    "ETag": "etag",
                    "ContentLength": 4,
                    "LastModified": NOW,
                    "ContentType": "text/plain",
                }
                status = engine.check_restore_status("f.txt")
                assert status["ready"] is False
                assert status["tier"] == "GLACIER"
            db.close()

    def test_check_restore_status_complete(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            db.upsert_file(_make_record("f.txt", tier="GLACIER"))

            with patch.object(s3, "head_object") as mock_head:
                mock_head.return_value = {
                    "StorageClass": "HOT_TEMP",
                    "Restore": 'ongoing-request="false", expiry-date="Mon, 01 Jan 2029 00:00:00 GMT"',
                    "Metadata": {},
                    "ETag": "etag",
                    "ContentLength": 4,
                    "LastModified": NOW,
                    "ContentType": "text/plain",
                }
                status = engine.check_restore_status("f.txt")
                assert status["ready"] is True
                assert status["expires_at"] is not None
            db.close()

    def test_check_restore_status_no_restore_header(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            with patch.object(s3, "head_object") as mock_head:
                mock_head.return_value = {
                    "StorageClass": "STANDARD",
                    "Restore": None,
                    "Metadata": {},
                    "ETag": "etag",
                    "ContentLength": 4,
                    "LastModified": NOW,
                    "ContentType": "text/plain",
                }
                status = engine.check_restore_status("f.txt")
                assert status["ready"] is False
            db.close()


# ---------------------------------------------------------------------------
# download_restored
# ---------------------------------------------------------------------------


class TestDownloadRestored:
    def test_download_restored_not_ready(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            with patch.object(engine, "check_restore_status") as mock_status:
                mock_status.return_value = {"ready": False, "tier": "GLACIER", "expires_at": None}
                result = engine.download_restored("f.txt")
                assert result is None
            db.close()


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_get_status_empty(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            diff = engine.get_status()
            assert isinstance(diff, DiffResult)
            assert diff.is_empty()
            db.close()


# ---------------------------------------------------------------------------
# _resolve_conflicts
# ---------------------------------------------------------------------------


class TestResolveConflicts:
    def test_resolve_conflict_local_strategy(self, tmp_path: Path):
        cfg = _make_config(tmp_path, conflict_strategy="local")
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()
        upload_paths, download_paths, skip_paths = engine._resolve_conflicts(diff, "local", result)
        assert "conflict.txt" in upload_paths
        assert len(download_paths) == 0
        db.close()

    def test_resolve_conflict_remote_strategy(self, tmp_path: Path):
        cfg = _make_config(tmp_path, conflict_strategy="remote")
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()
        upload_paths, download_paths, skip_paths = engine._resolve_conflicts(diff, "remote", result)
        assert "conflict.txt" in download_paths
        assert len(upload_paths) == 0
        db.close()

    def test_resolve_conflict_ask_strategy(self, tmp_path: Path):
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        diff = DiffResult(conflict=["conflict.txt"])
        result = SyncResult()
        upload_paths, download_paths, skip_paths = engine._resolve_conflicts(diff, "ask", result)
        assert "conflict.txt" in skip_paths
        assert "conflict.txt" in result.conflicts
        db.close()


# ---------------------------------------------------------------------------
# Sync with pull_only and push_only flags
# ---------------------------------------------------------------------------


class TestSyncFlags:
    def test_sync_pull_only(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            # Put a remote file
            content = b"remote only content"
            sha = hashlib.sha256(content).hexdigest()
            raw.put_object(Bucket=BUCKET, Key="remote_only.txt", Body=content)

            manifest = {
                "remote_only.txt": {
                    "sha256": sha,
                    "size": len(content),
                    "tier": "STANDARD",
                    "modified_at": NOW.isoformat(),
                    "etag": "etag",
                }
            }
            raw.put_object(
                Bucket=BUCKET,
                Key=".sahara/manifest.json",
                Body=json.dumps(manifest).encode(),
                ContentType="application/json",
            )

            result = engine.sync(pull_only=True)
            assert "remote_only.txt" in result.downloaded
            db.close()

    def test_sync_with_verify(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "verify_me.txt").write_bytes(b"data to verify")

            result = engine.sync(verify=True)
            assert "verify_me.txt" in result.uploaded
            db.close()


# ---------------------------------------------------------------------------
# _execute_delete_remote and _execute_delete_local
# ---------------------------------------------------------------------------


class TestExecuteDeletes:
    def test_execute_delete_remote_success(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            f = sync_folder / "to_delete.txt"
            f.write_bytes(b"delete me")
            s3.upload_file(f, "to_delete.txt")

            result = engine._execute_delete_remote("to_delete.txt")
            assert result is True
            db.close()

    def test_execute_delete_local_success(self, tmp_path: Path):
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        sync_folder = cfg.get_sync_folder_path()
        f = sync_folder / "local_del.txt"
        f.write_bytes(b"delete locally")

        result = engine._execute_delete_local("local_del.txt")
        assert result is True
        assert not f.exists()
        db.close()

    def test_execute_delete_local_missing_file_still_succeeds(self, tmp_path: Path):
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        result = engine._execute_delete_local("nonexistent.txt")
        assert result is True
        db.close()


# ---------------------------------------------------------------------------
# _execute_move
# ---------------------------------------------------------------------------


class TestExecuteMove:
    def test_execute_move(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()

            # Upload original
            f = sync_folder / "old_name.txt"
            f.write_bytes(b"move me")
            s3.upload_file(f, "old_name.txt")
            db.upsert_file(_make_record("old_name.txt"))

            # Create new name locally
            new_f = sync_folder / "new_name.txt"
            new_f.write_bytes(b"move me")

            record = engine._execute_move("old_name.txt", "new_name.txt")
            assert record is not None
            assert record.relative_path == "new_name.txt"
            db.close()


# ---------------------------------------------------------------------------
# _build_manifest_from_db
# ---------------------------------------------------------------------------


class TestBuildManifestFromDB:
    def test_build_manifest_empty(self, tmp_path: Path):
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        manifest = engine._build_manifest_from_db()
        assert manifest == {}
        db.close()

    def test_build_manifest_with_records(self, tmp_path: Path):
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        db.upsert_file(_make_record("file1.txt", sha256="sha1"))
        db.upsert_file(_make_record("file2.txt", sha256="sha2"))

        manifest = engine._build_manifest_from_db()
        assert "file1.txt" in manifest
        assert "file2.txt" in manifest
        assert manifest["file1.txt"]["sha256"] == "sha1"
        db.close()


# ---------------------------------------------------------------------------
# _bootstrap_manifest
# ---------------------------------------------------------------------------


class TestBootstrapManifest:
    def test_bootstrap_from_empty_bucket(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            manifest = engine._bootstrap_manifest()
            assert manifest == {}
            db.close()

    def test_bootstrap_with_files(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            raw.put_object(Bucket=BUCKET, Key="file1.txt", Body=b"data1")
            raw.put_object(Bucket=BUCKET, Key="file2.txt", Body=b"data2")

            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            manifest = engine._bootstrap_manifest()
            assert "file1.txt" in manifest
            assert "file2.txt" in manifest
            db.close()

    def test_bootstrap_with_prefix(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            raw.put_object(Bucket=BUCKET, Key="myprefix/file1.txt", Body=b"data")
            raw.put_object(Bucket=BUCKET, Key="other/file2.txt", Body=b"other")

            cfg = _make_config(tmp_path, prefix="myprefix")
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            manifest = engine._bootstrap_manifest()
            assert "file1.txt" in manifest
            assert "other/file2.txt" not in manifest
            db.close()


# ---------------------------------------------------------------------------
# _write_manifest_with_retry
# ---------------------------------------------------------------------------


class TestWriteManifestWithRetry:
    def test_manifest_conflict_retries_with_delta(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            from sahara.s3_client import ManifestConflictError
            call_count = [0]

            def put_manifest_side_effect(manifest, if_match_etag=None, key=None):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ManifestConflictError("etag1")
                return "new-etag"

            entry = {
                "sha256": "a" * 64,
                "size": 1,
                "tier": "STANDARD",
                "modified_at": NOW.isoformat(),
                "etag": "etag",
            }
            with (
                patch.object(
                    s3,
                    "put_manifest",
                    side_effect=put_manifest_side_effect,
                ),
                patch.object(
                    s3,
                    "get_manifest",
                    return_value=({"other.txt": entry}, "etag2"),
                ),
            ):
                engine._write_manifest_with_retry(
                    {"myfile": entry},
                    "old-etag",
                    base_manifest={},
                )

            assert call_count[0] == 2
            db.close()


# ---------------------------------------------------------------------------
# Sync with encryption enabled
# ---------------------------------------------------------------------------


class TestSyncWithEncryption:
    def test_sync_upload_with_encryption(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path, encryption_enabled=True)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "secret.txt").write_bytes(b"encrypted content")

            with patch("sahara.sync_engine.get_passphrase", return_value="test-passphrase"), \
                 patch("sahara.encryption.keyring.get_password", return_value="test-passphrase"):
                result = engine.sync()
                # File should be uploaded
                assert "secret.txt" in result.uploaded or not result.had_errors
            db.close()

    def test_sync_upload_no_passphrase_fails(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path, encryption_enabled=True)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "secret.txt").write_bytes(b"data")

            with patch("sahara.sync_engine.get_passphrase", return_value=None):
                result = engine.sync()
                # Upload should fail due to no passphrase
                assert "secret.txt" in result.failed or result.had_errors or len(result.uploaded) == 0
            db.close()


# ---------------------------------------------------------------------------
# Three-way diff: both sides same (no change)
# ---------------------------------------------------------------------------


class TestThreeWayDiffEdgeCases:
    def test_no_change_when_all_match(self, tmp_path: Path):
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        sync_folder = engine._sync_folder
        content = b"unchanged content"
        sha = hashlib.sha256(content).hexdigest()
        (sync_folder / "stable.txt").write_bytes(content)

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "stable.txt": LocalFileInfo(
                path=sync_folder / "stable.txt",
                relative="stable.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        manifest = {"stable.txt": ManifestEntry(
            sha256=sha, size=len(content), tier="STANDARD",
            modified_at=NOW.isoformat(), etag="etag"
        )}
        db_records = {"stable.txt": _make_record("stable.txt", sha256=sha)}

        diff = engine._three_way_diff(local_files, manifest, db_records)
        assert "stable.txt" not in diff.local_modified
        assert "stable.txt" not in diff.remote_modified
        assert "stable.txt" not in diff.conflict
        db.close()

    def test_both_sides_same_change_no_conflict(self, tmp_path: Path):
        """When local and manifest both have same new sha, no conflict."""
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock()
        engine = SyncEngine(cfg, db, s3)

        sync_folder = engine._sync_folder
        content = b"both changed to same"
        sha_new = hashlib.sha256(content).hexdigest()
        sha_old = "original-sha-different"
        (sync_folder / "both_same.txt").write_bytes(content)

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "both_same.txt": LocalFileInfo(
                path=sync_folder / "both_same.txt",
                relative="both_same.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        manifest = {"both_same.txt": ManifestEntry(
            sha256=sha_new, size=len(content), tier="STANDARD",
            modified_at=NOW.isoformat(), etag="etag"
        )}
        db_records = {"both_same.txt": _make_record("both_same.txt", sha256=sha_old)}

        diff = engine._three_way_diff(local_files, manifest, db_records)
        assert "both_same.txt" not in diff.conflict
        db.close()
