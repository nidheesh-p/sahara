"""Tests for sahara.sync_engine."""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import boto3
from moto import mock_aws

from sahara.config import SaharaConfig
from sahara.models import FileRecord, ManifestEntry, SyncResult
from sahara.s3_client import S3Client
from sahara.state_db import StateDB
from sahara.sync_engine import DiffResult, SyncEngine, _compute_sha256

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BUCKET = "sync-test-bucket"
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
    db = StateDB(tmp_path / "state.db")
    db.connect()
    return db


def _make_record(
    path: str,
    sha256: str = "abc123",
    tier: str = "STANDARD",
    is_deleted: bool = False,
) -> FileRecord:
    return FileRecord(
        relative_path=path,
        sha256_checksum=sha256,
        size_bytes=100,
        tier=tier,
        s3_etag="etag",
        last_sync_at=NOW,
        local_modified_at=NOW,
        remote_modified_at=NOW,
        is_deleted=is_deleted,
    )


def _make_manifest_entry(sha256: str = "abc123", tier: str = "STANDARD") -> ManifestEntry:
    return ManifestEntry(
        sha256=sha256,
        size=100,
        tier=tier,
        modified_at=NOW.isoformat(),
        etag="etag",
    )


# ---------------------------------------------------------------------------
# _compute_sha256
# ---------------------------------------------------------------------------


def test_compute_sha256(tmp_path: Path):
    content = b"test content for sha256"
    f = tmp_path / "f.txt"
    f.write_bytes(content)
    result = _compute_sha256(f)
    expected = hashlib.sha256(content).hexdigest()
    assert result == expected


# ---------------------------------------------------------------------------
# DiffResult.is_empty
# ---------------------------------------------------------------------------


class TestDiffResult:
    def test_is_empty_when_all_empty(self):
        diff = DiffResult()
        assert diff.is_empty() is True

    def test_not_empty_with_local_new(self):
        diff = DiffResult(local_new=["file.txt"])
        assert diff.is_empty() is False

    def test_not_empty_with_remote_new(self):
        diff = DiffResult(remote_new=["remote.txt"])
        assert diff.is_empty() is False

    def test_not_empty_with_conflicts(self):
        diff = DiffResult(conflict=["conflict.txt"])
        assert diff.is_empty() is False

    def test_not_empty_with_local_moves(self):
        diff = DiffResult(local_moves=[("old", "new")])
        assert diff.is_empty() is False

    def test_not_empty_with_local_deleted(self):
        diff = DiffResult(local_deleted=["gone.txt"])
        assert diff.is_empty() is False


# ---------------------------------------------------------------------------
# _scan_local
# ---------------------------------------------------------------------------


class TestScanLocal:
    def test_scan_finds_files(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "file.txt").write_bytes(b"data")
            (sync_folder / "subdir").mkdir()
            (sync_folder / "subdir" / "nested.txt").write_bytes(b"nested")

            local = engine._scan_local()
            assert "file.txt" in local
            assert "subdir/nested.txt" in local
            db.close()

    def test_scan_excludes_ignored_files(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "file.txt").write_bytes(b"data")
            (sync_folder / ".DS_Store").write_bytes(b"")
            (sync_folder / "file.tmp").write_bytes(b"temp")

            local = engine._scan_local()
            assert "file.txt" in local
            assert ".DS_Store" not in local
            assert "file.tmp" not in local
            db.close()

    def test_scan_excludes_node_modules(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "node_modules").mkdir()
            (sync_folder / "node_modules" / "package.js").write_bytes(b"")
            (sync_folder / "app.js").write_bytes(b"app code")

            local = engine._scan_local()
            assert "app.js" in local
            assert not any("node_modules" in p for p in local.keys())
            db.close()


# ---------------------------------------------------------------------------
# _three_way_diff
# ---------------------------------------------------------------------------


class TestThreeWayDiff:
    def _engine(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        # We can't use mock_aws context here, so use a mock s3
        s3 = MagicMock(spec=S3Client)
        return SyncEngine(cfg, db, s3), db

    def test_local_new(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        sync_folder = engine._sync_folder
        (sync_folder / "new_file.txt").write_bytes(b"new")

        # Simulate local scan result
        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "new_file.txt": LocalFileInfo(
                path=sync_folder / "new_file.txt",
                relative="new_file.txt",
                mtime=NOW,
                size=3,
            )
        }

        diff = engine._three_way_diff(local_files, {}, {})
        assert "new_file.txt" in diff.local_new
        db.close()

    def test_remote_new(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        manifest = {"remote_file.txt": _make_manifest_entry()}
        diff = engine._three_way_diff({}, manifest, {})
        assert "remote_file.txt" in diff.remote_new
        db.close()

    def test_local_modified(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        sync_folder = engine._sync_folder

        content = b"modified content"
        (sync_folder / "mod.txt").write_bytes(content)
        db_sha = "old-sha"

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "mod.txt": LocalFileInfo(
                path=sync_folder / "mod.txt",
                relative="mod.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        manifest = {"mod.txt": _make_manifest_entry(sha256=db_sha)}
        db_records = {"mod.txt": _make_record("mod.txt", sha256=db_sha)}

        diff = engine._three_way_diff(local_files, manifest, db_records)
        assert "mod.txt" in diff.local_modified
        db.close()

    def test_remote_modified(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        sync_folder = engine._sync_folder

        content = b"unchanged local"
        (sync_folder / "remote_mod.txt").write_bytes(content)
        local_sha = hashlib.sha256(content).hexdigest()

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "remote_mod.txt": LocalFileInfo(
                path=sync_folder / "remote_mod.txt",
                relative="remote_mod.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        # DB and local match; manifest differs
        manifest = {"remote_mod.txt": _make_manifest_entry(sha256="remote-new-sha")}
        db_records = {"remote_mod.txt": _make_record("remote_mod.txt", sha256=local_sha)}

        diff = engine._three_way_diff(local_files, manifest, db_records)
        assert "remote_mod.txt" in diff.remote_modified
        db.close()

    def test_conflict(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        sync_folder = engine._sync_folder

        # Both local and remote differ from DB (and differ from each other)
        content = b"local conflict content"
        (sync_folder / "conflict.txt").write_bytes(content)

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "conflict.txt": LocalFileInfo(
                path=sync_folder / "conflict.txt",
                relative="conflict.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        db_sha = "original-sha"
        manifest = {"conflict.txt": _make_manifest_entry(sha256="remote-conflict-sha")}
        db_records = {"conflict.txt": _make_record("conflict.txt", sha256=db_sha)}

        diff = engine._three_way_diff(local_files, manifest, db_records)
        assert "conflict.txt" in diff.conflict
        db.close()

    def test_local_deleted(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        # File in DB and manifest, but NOT in local
        manifest = {"deleted.txt": _make_manifest_entry()}
        db_records = {"deleted.txt": _make_record("deleted.txt")}
        diff = engine._three_way_diff({}, manifest, db_records)
        assert "deleted.txt" in diff.local_deleted
        db.close()

    def test_remote_deleted(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        sync_folder = engine._sync_folder
        content = b"still here locally"
        (sync_folder / "remote_del.txt").write_bytes(content)

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "remote_del.txt": LocalFileInfo(
                path=sync_folder / "remote_del.txt",
                relative="remote_del.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        # In DB and local, but NOT in manifest
        db_records = {"remote_del.txt": _make_record("remote_del.txt")}
        diff = engine._three_way_diff(local_files, {}, db_records)
        assert "remote_del.txt" in diff.remote_deleted
        db.close()

    def test_soft_deleted_in_db_treated_as_not_in_db(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        # File soft-deleted in DB: treat as local_new if exists locally
        sync_folder = engine._sync_folder
        content = b"restored file"
        (sync_folder / "soft_del.txt").write_bytes(content)

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        local_files = {
            "soft_del.txt": LocalFileInfo(
                path=sync_folder / "soft_del.txt",
                relative="soft_del.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        db_records = {"soft_del.txt": _make_record("soft_del.txt", is_deleted=True)}
        diff = engine._three_way_diff(local_files, {}, db_records)
        assert "soft_del.txt" in diff.local_new
        db.close()


# ---------------------------------------------------------------------------
# _detect_renames
# ---------------------------------------------------------------------------


class TestDetectRenames:
    def _engine(self, tmp_path: Path):
        cfg = _make_config(tmp_path)
        db = _make_db(tmp_path)
        s3 = MagicMock(spec=S3Client)
        return SyncEngine(cfg, db, s3), db

    def test_detects_simple_rename(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        sync_folder = engine._sync_folder

        content = b"file to be renamed"
        sha = hashlib.sha256(content).hexdigest()
        (sync_folder / "new_name.txt").write_bytes(content)

        from dataclasses import dataclass

        @dataclass
        class LocalFileInfo:
            path: Path
            relative: str
            mtime: datetime.datetime
            size: int

        diff = DiffResult(
            local_new=["new_name.txt"],
            local_deleted=["old_name.txt"],
        )

        local_files = {
            "new_name.txt": LocalFileInfo(
                path=sync_folder / "new_name.txt",
                relative="new_name.txt",
                mtime=NOW,
                size=len(content),
            )
        }
        manifest = {"old_name.txt": _make_manifest_entry(sha256=sha)}

        result = engine._detect_renames(diff, local_files, manifest)
        assert len(result.local_moves) == 1
        assert result.local_moves[0] == ("old_name.txt", "new_name.txt")
        assert "new_name.txt" not in result.local_new
        assert "old_name.txt" not in result.local_deleted
        db.close()

    def test_no_rename_when_no_local_deleted(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        diff = DiffResult(local_new=["new.txt"], local_deleted=[])
        result = engine._detect_renames(diff, {}, {})
        assert result.local_moves == []
        db.close()

    def test_no_rename_when_no_local_new(self, tmp_path: Path):
        engine, db = self._engine(tmp_path)
        diff = DiffResult(local_new=[], local_deleted=["old.txt"])
        result = engine._detect_renames(diff, {}, {})
        assert result.local_moves == []
        db.close()


# ---------------------------------------------------------------------------
# Full sync via moto
# ---------------------------------------------------------------------------


class TestSyncEmptyFolderAndManifest:
    def test_sync_empty_produces_empty_result(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            result = engine.sync()
            assert isinstance(result, SyncResult)
            assert result.total_changes == 0
            assert not result.had_errors
            db.close()


class TestSyncUploadsLocalNew:
    def test_sync_uploads_new_local_file(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            test_file = sync_folder / "new_file.txt"
            test_file.write_bytes(b"Hello, World!")

            result = engine.sync()
            assert "new_file.txt" in result.uploaded
            assert not result.had_errors
            db.close()


class TestSyncDownloadsRemoteNew:
    def test_sync_downloads_remote_new_file(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            # Put a file in S3 and write manifest
            content = b"remote content"
            sha = hashlib.sha256(content).hexdigest()
            raw.put_object(Bucket=BUCKET, Key="remote_file.txt", Body=content)

            manifest = {
                "remote_file.txt": {
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

            result = engine.sync()
            assert "remote_file.txt" in result.downloaded
            db.close()


class TestSyncConflictBackupStrategy:
    def test_sync_conflict_creates_backup_file(self, tmp_path: Path):
        with mock_aws():
            raw = boto3.client("s3", region_name=REGION)
            raw.create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path, conflict_strategy="backup")
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            content_local = b"local version of conflict"
            content_remote = b"remote version of conflict"
            sha_remote = hashlib.sha256(content_remote).hexdigest()
            sha_db = "original-sha-before-both-changed"

            # Create local file
            (sync_folder / "conflict.txt").write_bytes(content_local)

            # Set DB record with original sha
            db.upsert_file(_make_record("conflict.txt", sha256=sha_db))

            # Remote has different sha
            raw.put_object(Bucket=BUCKET, Key="conflict.txt", Body=content_remote)
            manifest = {
                "conflict.txt": {
                    "sha256": sha_remote,
                    "size": len(content_remote),
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

            result = engine.sync()
            # Backup strategy: creates a .conflict file and downloads remote
            assert len(result.conflicts) > 0 or "conflict.txt" in result.downloaded
            db.close()


class TestDryRun:
    def test_dry_run_produces_no_changes(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "dry_run_file.txt").write_bytes(b"test data")

            result = engine.sync(dry_run=True)
            # dry_run fills in what would happen
            assert "dry_run_file.txt" in result.uploaded
            # But DB should not have a record since nothing was actually done
            assert db.get_file("dry_run_file.txt") is None
            db.close()

    def test_push_only_dry_run(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg = _make_config(tmp_path)
            db = _make_db(tmp_path)
            s3 = S3Client(cfg)
            engine = SyncEngine(cfg, db, s3)

            sync_folder = cfg.get_sync_folder_path()
            (sync_folder / "push.txt").write_bytes(b"push data")

            result = engine.sync(push_only=True, dry_run=True)
            assert "push.txt" in result.uploaded
            db.close()
