"""Extended CLI tests targeting uncovered code paths."""
from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig, save_config
from sahara.models import FileRecord
from sahara.sync_engine import DiffResult

BUCKET = "cli-ext-bucket"
REGION = "us-east-1"
NOW = datetime.datetime.now(datetime.UTC)


def _runner():
    return CliRunner()


def _make_config(tmp_path: Path, **kwargs) -> tuple[SaharaConfig, Path]:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir(parents=True, exist_ok=True)
    cfg = SaharaConfig(
        sync_folder=str(sync_folder),
        bucket=BUCKET,
        region=REGION,
        **kwargs,
    )
    config_path = tmp_path / "config.toml"
    save_config(cfg, config_path)
    return cfg, config_path


def _make_mock_db(records=None, history=None):
    mock_db = MagicMock()
    mock_db.connect.return_value = mock_db
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    mock_db.list_files.return_value = records or []
    mock_db.list_files_by_tier.return_value = records or []
    mock_db.get_history.return_value = history or []
    mock_db.get_total_size_by_tier.return_value = {}
    mock_db.list_pending_restores.return_value = []
    return mock_db


def _make_mock_s3():
    mock_s3 = MagicMock()
    mock_s3.get_manifest.return_value = ({}, "etag")
    mock_s3.list_multipart_uploads.return_value = []
    return mock_s3


def _make_mock_engine():
    mock_engine = MagicMock()
    mock_engine.get_status.return_value = DiffResult()
    return mock_engine


def _make_sync_result(**kwargs):
    from sahara.models import SyncResult
    result = SyncResult()
    result.uploaded = kwargs.get("uploaded", [])
    result.downloaded = kwargs.get("downloaded", [])
    result.deleted = kwargs.get("deleted", [])
    result.conflicts = kwargs.get("conflicts", [])
    result.moved = kwargs.get("moved", [])
    result.failed = kwargs.get("failed", [])
    return result


def _make_file_record(path="file.txt", tier="STANDARD"):
    return FileRecord(
        relative_path=path,
        sha256_checksum="abc123",
        size_bytes=1024,
        tier=tier,
        s3_etag="etag",
        last_sync_at=NOW,
        local_modified_at=NOW,
        remote_modified_at=NOW,
    )


# ---------------------------------------------------------------------------
# sync / push / pull commands
# ---------------------------------------------------------------------------


class TestSyncCommand:
    def test_sync_success(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result(uploaded=["a.txt"])

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "sync"])
            assert result.exit_code == 0

    def test_sync_dry_run(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "sync", "--dry-run"])
            assert result.exit_code == 0
            assert "DRY RUN" in result.output

    def test_sync_with_errors(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result(
            failed=[("file.txt", "S3 error")]
        )

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "sync"])
            assert result.exit_code == 0
            assert "Error" in result.output or "error" in result.output.lower()

    def test_sync_no_config_aborts(self, tmp_path: Path):
        runner = _runner()
        empty = tmp_path / "empty.toml"
        save_config(SaharaConfig(), empty)
        result = runner.invoke(main, ["--config", str(empty), "sync"])
        assert result.exit_code != 0 or "not initialised" in result.output.lower()

    def test_push_command(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "push"])
            assert result.exit_code == 0
            # Verify sync was called with push_only=True
            call_kwargs = mock_engine.sync.call_args[1]
            assert call_kwargs.get("push_only") is True

    def test_pull_command(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "pull"])
            assert result.exit_code == 0
            call_kwargs = mock_engine.sync.call_args[1]
            assert call_kwargs.get("pull_only") is True

    def test_sync_with_conflicts_and_ask_strategy(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path, conflict_strategy="ask")
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result(conflicts=["conflict.txt"])

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "sync"])
            assert result.exit_code == 0
            assert "conflict" in result.output.lower()

    def test_sync_engine_raises_aborts(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.side_effect = RuntimeError("Unexpected failure")

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "sync"])
            assert result.exit_code != 0 or "Sync failed" in result.output


# ---------------------------------------------------------------------------
# rm command
# ---------------------------------------------------------------------------


class TestRmCommand:
    def test_rm_force_deletes_file(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        # Create a local file to delete
        local_file = cfg.get_sync_folder_path() / "file.txt"
        local_file.write_text("hello")

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "rm", "file.txt", "--force"]
            )
            assert result.exit_code == 0
            mock_s3.delete_object.assert_called_once()

    def test_rm_local_only(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        local_file = cfg.get_sync_folder_path() / "file.txt"
        local_file.write_text("hello")

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "rm", "file.txt", "--force", "--local"]
            )
            assert result.exit_code == 0
            # S3 delete should NOT be called for local-only
            mock_s3.delete_object.assert_not_called()

    def test_rm_no_config_aborts(self, tmp_path: Path):
        runner = _runner()
        empty = tmp_path / "empty.toml"
        save_config(SaharaConfig(), empty)
        result = runner.invoke(
            main, ["--config", str(empty), "rm", "file.txt", "--force"]
        )
        assert result.exit_code != 0 or "not initialised" in result.output.lower()

    def test_rm_with_confirmation_prompt(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            # Answer 'y' to the confirmation prompt
            result = runner.invoke(
                main, ["--config", str(config_path), "rm", "file.txt"],
                input="y\n"
            )
            assert result.exit_code == 0

    def test_rm_with_confirmation_denied(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            # Answer 'n' to the confirmation prompt
            result = runner.invoke(
                main, ["--config", str(config_path), "rm", "file.txt"],
                input="n\n"
            )
            assert result.exit_code == 0
            # S3 delete should NOT be called
            mock_s3.delete_object.assert_not_called()

    def test_rm_s3_delete_fails_aborts(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_s3.delete_object.side_effect = RuntimeError("S3 error")

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "rm", "file.txt", "--force"]
            )
            assert result.exit_code != 0 or "S3 delete failed" in result.output


# ---------------------------------------------------------------------------
# mv command
# ---------------------------------------------------------------------------


class TestMvCommand:
    def test_mv_renames_file(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        src_file = cfg.get_sync_folder_path() / "old.txt"
        src_file.write_text("hello")

        mock_db = _make_mock_db()
        mock_db.get_file.return_value = None
        mock_s3 = _make_mock_s3()
        mock_s3.copy_object.return_value = "newtag"

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "mv", "old.txt", "new.txt"]
            )
            assert result.exit_code == 0
            assert "new.txt" in result.output

    def test_mv_source_not_found_aborts(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "mv", "nonexistent.txt", "new.txt"]
            )
            assert result.exit_code != 0 or "not found" in result.output.lower()

    def test_mv_no_config_aborts(self, tmp_path: Path):
        runner = _runner()
        empty = tmp_path / "empty.toml"
        save_config(SaharaConfig(), empty)
        result = runner.invoke(
            main, ["--config", str(empty), "mv", "src.txt", "dst.txt"]
        )
        assert result.exit_code != 0 or "not initialised" in result.output.lower()

    def test_mv_with_existing_db_record(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        src_file = cfg.get_sync_folder_path() / "old.txt"
        src_file.write_text("hello")

        existing_record = _make_file_record("old.txt")
        mock_db = _make_mock_db()
        mock_db.get_file.return_value = existing_record
        mock_s3 = _make_mock_s3()
        mock_s3.copy_object.return_value = "newtag"

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "mv", "old.txt", "new.txt"]
            )
            assert result.exit_code == 0
            mock_db.delete_file.assert_called_once_with("old.txt")


# ---------------------------------------------------------------------------
# archive command
# ---------------------------------------------------------------------------


class TestArchiveCommand:
    def test_archive_dry_run(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        records = [_make_file_record("file1.txt"), _make_file_record("file2.txt")]
        mock_db = _make_mock_db(records=records)
        mock_db.list_files_by_tier.return_value = records
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.archive_files.return_value = ["file1.txt", "file2.txt"]

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main,
                ["--config", str(config_path), "archive", "file1.txt", "file2.txt", "--dry-run"]
            )
            assert result.exit_code == 0
            assert "DRY RUN" in result.output

    def test_archive_with_force(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.archive_files.return_value = ["file.txt"]

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main,
                ["--config", str(config_path), "archive", "file.txt", "--force"]
            )
            assert result.exit_code == 0
            assert "Archived" in result.output

    def test_archive_older_than(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        old_time = NOW - datetime.timedelta(days=100)
        old_record = FileRecord(
            relative_path="old_file.txt",
            sha256_checksum="abc",
            size_bytes=100,
            tier="STANDARD",
            s3_etag="etag",
            last_sync_at=old_time,
            local_modified_at=old_time,
            remote_modified_at=old_time,
        )
        mock_db = _make_mock_db(records=[old_record])
        mock_db.list_files_by_tier.return_value = [old_record]
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.archive_files.return_value = ["old_file.txt"]

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main,
                ["--config", str(config_path), "archive", "--older-than", "30", "--force"]
            )
            assert result.exit_code == 0

    def test_archive_no_files(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db(records=[])
        mock_db.list_files_by_tier.return_value = []
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main,
                ["--config", str(config_path), "archive", "--older-than", "30"]
            )
            assert result.exit_code == 0
            assert "No files" in result.output

    def test_archive_no_config_aborts(self, tmp_path: Path):
        runner = _runner()
        empty = tmp_path / "empty.toml"
        save_config(SaharaConfig(), empty)
        result = runner.invoke(
            main, ["--config", str(empty), "archive", "file.txt", "--force"]
        )
        assert result.exit_code != 0 or "not initialised" in result.output.lower()


# ---------------------------------------------------------------------------
# restore / restore-status / restore-download
# ---------------------------------------------------------------------------


class TestRestoreCommands:
    def test_restore_command(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.request_restore.return_value = None

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore", "archived.zip"]
            )
            assert result.exit_code == 0
            assert "Restore requested" in result.output

    def test_restore_with_options(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore", "file.zip",
                       "--days", "14", "--tier", "Expedited"]
            )
            assert result.exit_code == 0
            call_args = mock_engine.request_restore.call_args
            assert call_args[1].get("days") == 14 or call_args[0][1] == 14

    def test_restore_fails_aborts(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.request_restore.side_effect = RuntimeError("S3 error")

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore", "file.zip"]
            )
            assert result.exit_code != 0 or "failed" in result.output.lower()

    def test_restore_no_config_aborts(self, tmp_path: Path):
        runner = _runner()
        empty = tmp_path / "empty.toml"
        save_config(SaharaConfig(), empty)
        result = runner.invoke(
            main, ["--config", str(empty), "restore", "file.zip"]
        )
        assert result.exit_code != 0 or "not initialised" in result.output.lower()

    def test_restore_status_specific_path(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.check_restore_status.return_value = {
            "path": "file.zip",
            "tier": "GLACIER",
            "restore_header": 'ongoing-request="true"',
            "ready": False,
            "expires_at": None,
        }

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore-status", "file.zip"]
            )
            assert result.exit_code == 0
            assert "PENDING" in result.output

    def test_restore_status_ready_file(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.check_restore_status.return_value = {
            "path": "file.zip",
            "tier": "GLACIER",
            "restore_header": 'ongoing-request="false"',
            "ready": True,
            "expires_at": "2027-01-01T00:00:00+00:00",
        }

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore-status", "file.zip"]
            )
            assert result.exit_code == 0
            assert "READY" in result.output

    def test_restore_status_all_pending(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        pending_record = _make_file_record("pending.zip", tier="GLACIER")
        pending_record.restore_job_id = "job-123"
        mock_db = _make_mock_db()
        mock_db.list_pending_restores.return_value = [pending_record]
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.check_restore_status.return_value = {
            "path": "pending.zip",
            "tier": "GLACIER",
            "restore_header": 'ongoing-request="true"',
            "ready": False,
            "expires_at": None,
        }

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore-status"]
            )
            assert result.exit_code == 0

    def test_restore_status_no_pending(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_db.list_pending_restores.return_value = []
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore-status"]
            )
            assert result.exit_code == 0
            assert "No pending" in result.output

    def test_restore_download_ready(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.download_restored.return_value = "sha256hash"

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore-download", "file.zip"]
            )
            assert result.exit_code == 0
            assert "Downloaded" in result.output

    def test_restore_download_not_ready(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.download_restored.return_value = None

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "restore-download", "file.zip"]
            )
            assert result.exit_code == 0
            assert "not yet available" in result.output


# ---------------------------------------------------------------------------
# resolve command
# ---------------------------------------------------------------------------


class TestResolveCommand:
    def test_resolve_with_path(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "resolve", "conflict.txt", "--keep", "local"]
            )
            assert result.exit_code == 0

    def test_resolve_without_path(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()
        mock_engine.sync.return_value = _make_sync_result()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "resolve", "--keep", "remote"]
            )
            assert result.exit_code == 0


# ---------------------------------------------------------------------------
# conflicts command with actual conflicts
# ---------------------------------------------------------------------------


class TestConflictsWithItems:
    def test_conflicts_shows_list(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        diff = DiffResult(conflict=["conflict1.txt", "conflict2.txt"])
        mock_engine = MagicMock()
        mock_engine.get_status.return_value = diff

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(
                main, ["--config", str(config_path), "conflicts"]
            )
            assert result.exit_code == 0
            assert "conflict1.txt" in result.output
            assert "conflict2.txt" in result.output


# ---------------------------------------------------------------------------
# status with all diff types
# ---------------------------------------------------------------------------


class TestStatusAllDiffTypes:
    def test_status_with_all_change_types(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        diff = DiffResult(
            local_new=["new.txt"],
            local_modified=["modified.txt"],
            remote_new=["remote_new.txt"],
            remote_modified=["remote_mod.txt"],
            local_deleted=["local_del.txt"],
            remote_deleted=["remote_del.txt"],
            conflict=["conflict.txt"],
            local_moves=[("old.txt", "moved.txt")],
        )
        mock_engine = MagicMock()
        mock_engine.get_status.return_value = diff

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "status"])
            assert result.exit_code == 0
            assert "new.txt" in result.output
            assert "modified.txt" in result.output
            assert "conflict.txt" in result.output


# ---------------------------------------------------------------------------
# doctor with more code paths
# ---------------------------------------------------------------------------


class TestDoctorExtended:
    def test_doctor_with_repair_creates_sync_folder(self, tmp_path: Path):
        sync_folder = tmp_path / "missing_sync"
        cfg = SaharaConfig(
            sync_folder=str(sync_folder),
            bucket=BUCKET,
            region=REGION,
        )
        config_path = tmp_path / "config.toml"
        save_config(cfg, config_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_s3.validate_bucket_access.return_value = None
        mock_s3.check_conditional_put_support.return_value = False
        mock_s3.list_multipart_uploads.return_value = []

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "doctor", "--repair"]
            )
            assert result.exit_code == 0
            # Sync folder should be created
            assert sync_folder.exists()

    def test_doctor_with_stale_multipart_uploads(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_s3.validate_bucket_access.return_value = None
        mock_s3.check_conditional_put_support.return_value = True
        mock_s3.list_multipart_uploads.return_value = [
            {"Key": "stale_upload.txt", "UploadId": "upload-123"}
        ]

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(main, ["--config", str(config_path), "doctor"])
            assert result.exit_code == 0
            assert "stale" in result.output.lower()

    def test_doctor_repairs_multipart_uploads(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_s3.validate_bucket_access.return_value = None
        mock_s3.check_conditional_put_support.return_value = True
        mock_s3.list_multipart_uploads.return_value = [
            {"Key": "stale.txt", "UploadId": "upload-abc"}
        ]

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(
                main, ["--config", str(config_path), "doctor", "--repair"]
            )
            assert result.exit_code == 0
            mock_s3.abort_multipart_upload.assert_called_once_with("stale.txt", "upload-abc")

    def test_doctor_with_s3_access_error(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_s3.validate_bucket_access.side_effect = RuntimeError("access denied")

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(main, ["--config", str(config_path), "doctor"])
            assert result.exit_code == 0
            assert "failed" in result.output.lower() or "issue" in result.output.lower()


# ---------------------------------------------------------------------------
# daemon commands
# ---------------------------------------------------------------------------


class TestDaemonCommandsExtended:
    def test_daemon_start_success(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.start_daemon") as mock_start:
            result = runner.invoke(main, ["daemon", "start"])
            assert result.exit_code == 0
            assert "started" in result.output.lower()
            mock_start.assert_called_once()

    def test_daemon_start_fails(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.start_daemon", side_effect=RuntimeError("fork failed")):
            result = runner.invoke(main, ["daemon", "start"])
            assert result.exit_code != 0 or "failed" in result.output.lower()

    def test_daemon_stop_success(self):
        runner = _runner()
        with patch("sahara.daemon.stop_daemon") as mock_stop:
            result = runner.invoke(main, ["daemon", "stop"])
            assert result.exit_code == 0
            assert "stopped" in result.output.lower()
            mock_stop.assert_called_once()

    def test_daemon_status_paused(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=True), \
             patch("sahara.daemon._read_pid", return_value=12345), \
             patch("sahara.daemon._is_paused", return_value=True):
            result = runner.invoke(main, ["daemon", "status"])
            assert result.exit_code == 0
            assert "paused" in result.output.lower()

    def test_daemon_start_with_autostart(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.start_daemon"), \
             patch("sahara.daemon.install_autostart", return_value="/path/to/plist"):
            result = runner.invoke(main, ["daemon", "start", "--autostart"])
            assert result.exit_code == 0
            assert "autostart" in result.output.lower() or "Autostart" in result.output

    def test_daemon_start_autostart_fails(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.start_daemon"), \
             patch("sahara.daemon.install_autostart", side_effect=RuntimeError("unsupported")):
            result = runner.invoke(main, ["daemon", "start", "--autostart"])
            assert result.exit_code == 0
            assert "failed" in result.output.lower()

    def test_daemon_logs_follow(self, tmp_path: Path):
        log_file = tmp_path / "daemon.log"
        log_file.write_text("test log\n")
        runner = _runner()

        with patch("sahara.daemon._LOG_FILE", log_file), \
             patch("subprocess.run") as mock_run:
            result = runner.invoke(main, ["daemon", "logs", "--follow"])
            assert result.exit_code == 0
            mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# encryption commands extended
# ---------------------------------------------------------------------------


class TestEncryptionCommandsExtended:
    def test_encryption_group_help(self):
        runner = _runner()
        result = runner.invoke(main, ["encryption", "--help"])
        assert result.exit_code == 0
        assert "encryption" in result.output.lower()

    def test_encryption_setup_success(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        with patch("sahara.encryption.keyring.set_password"):
            result = runner.invoke(
                main,
                ["--config", str(config_path), "encryption", "setup"],
                input="mypassphrase\nmypassphrase\n",
            )
            assert result.exit_code == 0
            assert "Encryption enabled" in result.output

    def test_encryption_rotate_no_existing_passphrase(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path, encryption_enabled=True)
        runner = _runner()
        with patch("sahara.encryption.keyring.get_password", return_value=None):
            result = runner.invoke(
                main, ["--config", str(config_path), "encryption", "rotate"]
            )
            # Should abort because no current passphrase
            assert result.exit_code != 0 or "No current passphrase" in result.output

    def test_encryption_rotate_user_cancels(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path, encryption_enabled=True)
        runner = _runner()
        with patch("sahara.encryption.keyring.get_password", return_value="oldpass"):
            result = runner.invoke(
                main, ["--config", str(config_path), "encryption", "rotate"],
                input="n\n",  # User says no to confirmation
            )
            assert result.exit_code == 0
