"""Tests for sahara.cli using Click's CliRunner."""
from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from click.testing import CliRunner
from moto import mock_aws

from sahara.cli import main
from sahara.config import SaharaConfig, save_config
from sahara.models import FileRecord
from sahara.sync_engine import DiffResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BUCKET = "cli-test-bucket"
REGION = "us-east-1"
NOW = datetime.datetime.now(datetime.UTC)


def _runner() -> CliRunner:
    return CliRunner()


def _make_config(tmp_path: Path, **kwargs) -> tuple[SaharaConfig, Path]:
    """Create a temp config file and return (config, path)."""
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
    """Create a MagicMock StateDB with sensible defaults."""
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
    """Create a MagicMock S3Client."""
    mock_s3 = MagicMock()
    mock_s3.get_manifest.return_value = ({}, "etag")
    mock_s3.list_multipart_uploads.return_value = []
    return mock_s3


def _make_mock_engine():
    """Create a MagicMock SyncEngine."""
    mock_engine = MagicMock()
    mock_engine.get_status.return_value = DiffResult()
    return mock_engine


# ---------------------------------------------------------------------------
# help / version
# ---------------------------------------------------------------------------


class TestHelp:
    def test_help_shows_help_text(self):
        runner = _runner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Sahara" in result.output
        assert "Usage" in result.output

    def test_no_args_shows_help(self):
        runner = _runner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_version_flag(self):
        runner = _runner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "sahara" in result.output.lower()


# ---------------------------------------------------------------------------
# config show
# ---------------------------------------------------------------------------


class TestConfigShow:
    def test_config_show_with_no_config_file(self, tmp_path: Path):
        runner = _runner()
        nonexistent = tmp_path / "nonexistent.toml"
        result = runner.invoke(main, ["--config", str(nonexistent), "config", "show"])
        assert result.exit_code == 0
        # Should show defaults
        assert "sync_folder" in result.output

    def test_config_show_with_config_file(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        result = runner.invoke(main, ["--config", str(config_path), "config", "show"])
        assert result.exit_code == 0
        assert BUCKET in result.output
        assert "sync_folder" in result.output


# ---------------------------------------------------------------------------
# config set / get
# ---------------------------------------------------------------------------


class TestConfigSetGet:
    def test_config_set_and_get_string(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        # Set a string value
        result = runner.invoke(
            main, ["--config", str(config_path), "config", "set", "region", "eu-west-1"]
        )
        assert result.exit_code == 0
        assert "eu-west-1" in result.output

        # Get it back
        result = runner.invoke(
            main, ["--config", str(config_path), "config", "get", "region"]
        )
        assert result.exit_code == 0
        assert "eu-west-1" in result.output

    def test_config_set_integer(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        result = runner.invoke(
            main, ["--config", str(config_path), "config", "set", "max_workers", "16"]
        )
        assert result.exit_code == 0

        result = runner.invoke(
            main, ["--config", str(config_path), "config", "get", "max_workers"]
        )
        assert "16" in result.output

    def test_config_set_bool(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        result = runner.invoke(
            main, ["--config", str(config_path), "config", "set", "encryption_enabled", "true"]
        )
        assert result.exit_code == 0

        result = runner.invoke(
            main, ["--config", str(config_path), "config", "get", "encryption_enabled"]
        )
        assert "True" in result.output

    def test_config_set_unknown_key_aborts(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        result = runner.invoke(
            main, ["--config", str(config_path), "config", "set", "nonexistent_key", "val"]
        )
        # Exit code non-zero or shows error
        assert result.exit_code != 0 or "Unknown" in result.output

    def test_config_set_rejects_unknown_answer_provider(self, tmp_path: Path):
        _, config_path = _make_config(tmp_path)
        result = _runner().invoke(
            main,
            [
                "--config",
                str(config_path),
                "config",
                "set",
                "answer_provider",
                "unknown",
            ],
        )
        assert result.exit_code != 0
        assert "ollama" in result.output
        assert "openai" in result.output

    def test_config_get_unknown_key_aborts(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        result = runner.invoke(
            main, ["--config", str(config_path), "config", "get", "nonexistent_key"]
        )
        assert result.exit_code != 0 or "Unknown" in result.output

    def test_config_set_float(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        result = runner.invoke(
            main, ["--config", str(config_path), "config", "set", "debounce_seconds", "3.0"]
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_with_no_bucket_shows_error(self, tmp_path: Path):
        """status requires config.bucket and config.sync_folder."""
        runner = _runner()
        # Use a config with empty bucket
        empty_config = tmp_path / "empty.toml"
        save_config(SaharaConfig(), empty_config)
        result = runner.invoke(main, ["--config", str(empty_config), "status"])
        # Should fail or show warning since bucket not configured
        assert "not initialised" in result.output.lower() or result.exit_code != 0

    def test_status_up_to_date(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "status"])
            assert result.exit_code == 0
            assert "up to date" in result.output.lower()

    def test_status_with_pending_changes(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        diff = DiffResult(local_new=["new_file.txt"])
        mock_engine = MagicMock()
        mock_engine.get_status.return_value = diff

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "status"])
            assert result.exit_code == 0
            assert "new_file.txt" in result.output


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


class TestLsCommand:
    def test_ls_with_empty_db(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        mock_db = _make_mock_db(records=[])

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["--config", str(config_path), "ls"])
            assert result.exit_code == 0
            assert "No files" in result.output

    def test_ls_with_files(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        records = [
            FileRecord(
                relative_path="file_a.txt",
                sha256_checksum="abc",
                size_bytes=1024,
                tier="STANDARD",
                s3_etag="etag",
                last_sync_at=NOW,
                local_modified_at=NOW,
                remote_modified_at=NOW,
            ),
        ]
        mock_db = _make_mock_db(records=records)

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["--config", str(config_path), "ls"])
            assert result.exit_code == 0
            assert "file_a.txt" in result.output

    def test_ls_long_flag(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        records = [
            FileRecord(
                relative_path="doc.pdf",
                sha256_checksum="deadbeef" * 8,
                size_bytes=2048,
                tier="STANDARD",
                s3_etag="etag",
                last_sync_at=NOW,
                local_modified_at=NOW,
                remote_modified_at=NOW,
            ),
        ]
        mock_db = _make_mock_db(records=records)

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["--config", str(config_path), "ls", "--long"])
            assert result.exit_code == 0
            assert "doc.pdf" in result.output

    def test_ls_with_tier_filter(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        mock_db = _make_mock_db(records=[])

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(
                main, ["--config", str(config_path), "ls", "--tier", "GLACIER"]
            )
            assert result.exit_code == 0

    def test_ls_with_prefix_filter(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        records = [
            FileRecord(
                relative_path="docs/file.txt",
                sha256_checksum="abc",
                size_bytes=512,
                tier="STANDARD",
                s3_etag="etag",
                last_sync_at=NOW,
                local_modified_at=NOW,
                remote_modified_at=NOW,
            ),
            FileRecord(
                relative_path="photos/img.jpg",
                sha256_checksum="xyz",
                size_bytes=1024,
                tier="STANDARD",
                s3_etag="etag2",
                last_sync_at=NOW,
                local_modified_at=NOW,
                remote_modified_at=NOW,
            ),
        ]
        mock_db = _make_mock_db(records=records)

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["--config", str(config_path), "ls", "docs/"])
            assert result.exit_code == 0
            assert "docs/file.txt" in result.output
            assert "photos/img.jpg" not in result.output


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_history_with_empty_db(self):
        runner = _runner()
        mock_db = _make_mock_db(history=[])

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["history"])
            assert result.exit_code == 0
            assert "No history" in result.output

    def test_history_shows_entries(self):
        runner = _runner()

        entries = [
            {
                "id": 1,
                "relative_path": "file.txt",
                "operation": "upload",
                "sha256": "abc",
                "size_bytes": 512,
                "tier": "STANDARD",
                "occurred_at": NOW.isoformat(),
                "details": None,
            }
        ]
        mock_db = _make_mock_db(history=entries)

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["history"])
            assert result.exit_code == 0
            assert "file.txt" in result.output
            assert "upload" in result.output

    def test_history_with_path_filter(self):
        runner = _runner()
        mock_db = _make_mock_db(history=[])

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["history", "specific/file.txt"])
            assert result.exit_code == 0
            # Verify get_history was called with the path
            mock_db.get_history.assert_called_once()
            call_args = mock_db.get_history.call_args
            assert "specific/file.txt" in str(call_args)

    def test_history_with_limit(self):
        runner = _runner()
        mock_db = _make_mock_db(history=[])

        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["history", "--limit", "10"])
            assert result.exit_code == 0


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


class TestUsage:
    def test_usage_with_simulate_flag(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        result = runner.invoke(
            main,
            [
                "--config", str(config_path),
                "usage", "--simulate",
                "--standard-gb", "100",
                "--glacier-gb", "50",
                "--deep-archive-gb", "0",
            ]
        )
        assert result.exit_code == 0
        assert "TOTAL" in result.output

    def test_usage_with_no_bucket_aborts(self, tmp_path: Path):
        runner = _runner()
        empty_config = tmp_path / "empty.toml"
        save_config(SaharaConfig(), empty_config)
        result = runner.invoke(main, ["--config", str(empty_config), "usage"])
        assert result.exit_code != 0 or "No bucket" in result.output

    def test_usage_with_bucket_and_db(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_db.get_total_size_by_tier.return_value = {"STANDARD": 1024 * 1024}
        mock_db.list_files.return_value = []
        mock_s3 = _make_mock_s3()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3):
            result = runner.invoke(main, ["--config", str(config_path), "usage"])
            assert result.exit_code == 0
            assert "Sahara" in result.output

    def test_usage_simulate_with_defaults(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        result = runner.invoke(
            main, ["--config", str(config_path), "usage", "--simulate",
                   "--standard-gb", "0", "--glacier-gb", "0", "--deep-archive-gb", "0"]
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_doctor_with_mocked_aws(self, tmp_path: Path):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            cfg, config_path = _make_config(tmp_path)
            runner = _runner()

            mock_db = _make_mock_db()

            with patch("sahara.state_db.StateDB", return_value=mock_db), \
                 patch("sahara.s3_client.S3Client") as mock_s3_cls:
                mock_s3 = _make_mock_s3()
                mock_s3_cls.return_value = mock_s3
                mock_s3.validate_bucket_access.return_value = None
                mock_s3.check_conditional_put_support.return_value = True
                mock_s3.list_multipart_uploads.return_value = []

                result = runner.invoke(main, ["--config", str(config_path), "doctor"])
                assert result.exit_code == 0
                assert "Doctor" in result.output

    def test_doctor_with_no_config(self, tmp_path: Path):
        runner = _runner()
        nonexistent = tmp_path / "no_config.toml"
        result = runner.invoke(main, ["--config", str(nonexistent), "doctor"])
        assert result.exit_code == 0
        # Should warn about missing config
        assert "not found" in result.output.lower() or "not configured" in result.output.lower()

    def test_doctor_basic_mode_needs_no_bucket(self, tmp_path: Path):
        runner = _runner()
        no_bucket_config = tmp_path / "cfg.toml"
        save_config(SaharaConfig(sync_folder=str(tmp_path / "sync")), no_bucket_config)
        (tmp_path / "sync").mkdir(exist_ok=True)

        mock_db = _make_mock_db()
        with patch("sahara.state_db.StateDB", return_value=mock_db):
            result = runner.invoke(main, ["--config", str(no_bucket_config), "doctor"])
            assert result.exit_code == 0
            assert "basic index-only mode" in result.output.lower()


# ---------------------------------------------------------------------------
# daemon status
# ---------------------------------------------------------------------------


class TestDaemonStatus:
    def test_daemon_status_when_not_running(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon._read_pid", return_value=None), \
             patch("sahara.daemon._is_paused", return_value=False):
            result = runner.invoke(main, ["daemon", "status"])
            assert result.exit_code == 0
            assert "Stopped" in result.output or "stopped" in result.output.lower()

    def test_daemon_status_shows_pid_and_log_paths(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon._read_pid", return_value=None), \
             patch("sahara.daemon._is_paused", return_value=False):
            result = runner.invoke(main, ["daemon", "status"])
            assert result.exit_code == 0
            assert "PID file" in result.output
            assert "Log file" in result.output

    def test_daemon_status_when_running(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=True), \
             patch("sahara.daemon._read_pid", return_value=12345), \
             patch("sahara.daemon._is_paused", return_value=False):
            result = runner.invoke(main, ["daemon", "status"])
            assert result.exit_code == 0
            assert "12345" in result.output

    def test_daemon_pause(self):
        runner = _runner()
        with patch("sahara.daemon.pause_daemon") as mock_pause:
            result = runner.invoke(main, ["daemon", "pause"])
            assert result.exit_code == 0
            mock_pause.assert_called_once()

    def test_daemon_resume(self):
        runner = _runner()
        with patch("sahara.daemon.resume_daemon") as mock_resume:
            result = runner.invoke(main, ["daemon", "resume"])
            assert result.exit_code == 0
            mock_resume.assert_called_once()

    def test_daemon_stop_not_running(self):
        runner = _runner()
        with patch("sahara.daemon.stop_daemon", side_effect=RuntimeError("not running")):
            result = runner.invoke(main, ["daemon", "stop"])
            assert result.exit_code != 0 or "failed" in result.output.lower()

    def test_daemon_start_already_running(self):
        runner = _runner()
        with patch("sahara.daemon.is_daemon_running", return_value=True):
            result = runner.invoke(main, ["daemon", "start"])
            assert result.exit_code == 0
            assert "already running" in result.output.lower()


# ---------------------------------------------------------------------------
# encryption commands
# ---------------------------------------------------------------------------


class TestEncryptionCommands:
    def test_encryption_group_help(self):
        runner = _runner()
        result = runner.invoke(main, ["encryption", "--help"])
        assert result.exit_code == 0
        assert "encryption" in result.output.lower()

    def test_encryption_setup(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()
        with patch("sahara.encryption.keyring.set_password"):
            result = runner.invoke(
                main,
                ["--config", str(config_path), "encryption", "setup"],
                input="testpass\ntestpass\n",
            )
            assert result.exit_code == 0


# ---------------------------------------------------------------------------
# diff (alias for status)
# ---------------------------------------------------------------------------


class TestDiffAlias:
    def test_diff_is_alias_for_status(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "diff"])
            assert result.exit_code == 0


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_conflicts_with_none(self, tmp_path: Path):
        cfg, config_path = _make_config(tmp_path)
        runner = _runner()

        mock_db = _make_mock_db()
        mock_s3 = _make_mock_s3()
        mock_engine = _make_mock_engine()

        with patch("sahara.state_db.StateDB", return_value=mock_db), \
             patch("sahara.s3_client.S3Client", return_value=mock_s3), \
             patch("sahara.sync_engine.SyncEngine", return_value=mock_engine), \
             patch("sahara.ignore_rules.IgnoreRules"):
            result = runner.invoke(main, ["--config", str(config_path), "conflicts"])
            assert result.exit_code == 0
            assert "No conflicts" in result.output


# ---------------------------------------------------------------------------
# daemon logs
# ---------------------------------------------------------------------------


class TestDaemonLogs:
    def test_daemon_logs_no_log_file(self, tmp_path: Path):
        runner = _runner()
        with patch("sahara.daemon._LOG_FILE", tmp_path / "nonexistent.log"):
            result = runner.invoke(main, ["daemon", "logs"])
            assert result.exit_code == 0
            assert "No daemon log" in result.output

    def test_daemon_logs_reads_file(self, tmp_path: Path):
        log_file = tmp_path / "daemon.log"
        log_file.write_text("2024-01-01 INFO test log line\n")
        runner = _runner()
        with patch("sahara.daemon._LOG_FILE", log_file):
            result = runner.invoke(main, ["daemon", "logs"])
            assert result.exit_code == 0
            assert "test log line" in result.output
