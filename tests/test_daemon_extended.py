"""Extended daemon tests covering more code paths."""
from __future__ import annotations

import datetime
import os
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from sahara.daemon import (
    _read_pid,
    _write_pid,
    _clear_pid,
    _is_paused,
    is_daemon_running,
    pause_daemon,
    resume_daemon,
    get_daemon_status,
    start_daemon,
    stop_daemon,
    poll_restores,
    poll_restore_expiries,
    install_autostart,
    uninstall_autostart,
)


# ---------------------------------------------------------------------------
# start_daemon (non-fork path)
# ---------------------------------------------------------------------------


class TestStartDaemon:
    def test_start_daemon_raises_if_already_running(self):
        with patch("sahara.daemon.is_daemon_running", return_value=True):
            with pytest.raises(RuntimeError, match="already running"):
                start_daemon()

    def test_start_daemon_windows_path(self):
        """On non-Windows, we verify the Windows path can be reached via mock."""
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.platform.system", return_value="Windows"), \
             patch("sahara.daemon._start_daemon_windows") as mock_win:
            start_daemon()
            mock_win.assert_called_once()

    def test_start_daemon_fork_parent_returns(self, tmp_path: Path):
        """Verify that parent process returns immediately after fork."""
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.platform.system", return_value="Linux"), \
             patch("os.fork", return_value=999):  # Simulate parent (pid != 0)
            # Parent should return without calling _daemon_main
            with patch("sahara.daemon._daemon_main") as mock_main:
                start_daemon()
                mock_main.assert_not_called()

    def test_start_daemon_fork_child_calls_daemon_main(self, tmp_path: Path):
        """Verify child process calls _daemon_main."""
        call_log = []

        def fake_fork():
            return 0  # Child returns 0

        def fake_daemon_main(config_path):
            call_log.append(("daemon_main", config_path))
            raise SystemExit(0)  # Exit as child would

        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.platform.system", return_value="Linux"), \
             patch("os.fork", side_effect=fake_fork), \
             patch("sahara.daemon._daemon_main", side_effect=fake_daemon_main), \
             patch("sahara.daemon._clear_pid"), \
             pytest.raises(SystemExit):
            start_daemon()

        assert len(call_log) == 1
        assert call_log[0][0] == "daemon_main"

    def test_start_daemon_child_handles_crash(self):
        """Verify daemon child handles exceptions and cleans up PID."""
        with patch("sahara.daemon.is_daemon_running", return_value=False), \
             patch("sahara.daemon.platform.system", return_value="Linux"), \
             patch("os.fork", return_value=0), \
             patch("sahara.daemon._daemon_main", side_effect=RuntimeError("crash")), \
             patch("sahara.daemon._clear_pid") as mock_clear, \
             pytest.raises(SystemExit):
            start_daemon()

        mock_clear.assert_called()


# ---------------------------------------------------------------------------
# _start_daemon_windows
# ---------------------------------------------------------------------------


class TestStartDaemonWindows:
    def test_start_daemon_windows_launches_subprocess(self):
        from sahara.daemon import _start_daemon_windows

        mock_proc = MagicMock()
        mock_proc.pid = 12345

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            _start_daemon_windows(None)
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            assert "-m" in args
            assert "sahara.daemon" in args

    def test_start_daemon_windows_with_config_path(self, tmp_path: Path):
        from sahara.daemon import _start_daemon_windows

        config_path = tmp_path / "config.toml"
        mock_proc = MagicMock()
        mock_proc.pid = 99

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            _start_daemon_windows(config_path)
            call_args = mock_popen.call_args[0][0]
            assert "--config" in call_args
            assert str(config_path) in call_args


# ---------------------------------------------------------------------------
# poll_restores — additional coverage
# ---------------------------------------------------------------------------


class TestPollRestoresExtended:
    def test_poll_restores_parses_expiry_date(self):
        """Test that expiry date is parsed from restore header."""
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        mock_s3._config.get_s3_key.return_value = "archive.zip"

        NOW = datetime.datetime.now(datetime.timezone.utc)
        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="archive.zip",
            sha256_checksum="sha",
            size_bytes=100,
            tier="GLACIER",
            s3_etag="etag",
            last_sync_at=NOW,
            local_modified_at=NOW,
            remote_modified_at=NOW,
            restore_job_id="job-123",
        )
        mock_db.list_pending_restores.return_value = [record]
        mock_s3.head_object.return_value = {
            "Restore": 'ongoing-request="false", expiry-date="Fri, 01 Jan 2027 00:00:00 GMT"',
            "StorageClass": "GLACIER",
        }

        with patch("sahara.notifier.notify_restore_complete") as mock_notify:
            poll_restores(mock_db, mock_s3)
            mock_notify.assert_called_once_with("archive.zip")
            # Record should have been updated with HOT_TEMP tier
            upserted = mock_db.upsert_file.call_args[0][0]
            assert upserted.tier == "HOT_TEMP"
            assert upserted.restore_job_id is None

    def test_poll_restores_with_bad_expiry_date(self):
        """Test that malformed expiry date doesn't crash."""
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        mock_s3._config.get_s3_key.return_value = "file.zip"

        NOW = datetime.datetime.now(datetime.timezone.utc)
        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="file.zip",
            sha256_checksum="sha",
            size_bytes=0,
            tier="GLACIER",
            s3_etag="etag",
            last_sync_at=NOW,
            local_modified_at=NOW,
            remote_modified_at=NOW,
            restore_job_id="job",
        )
        mock_db.list_pending_restores.return_value = [record]
        mock_s3.head_object.return_value = {
            "Restore": 'ongoing-request="false", expiry-date="NOT A DATE"',
            "StorageClass": "GLACIER",
        }

        with patch("sahara.notifier.notify_restore_complete"):
            # Should not raise even with bad date
            poll_restores(mock_db, mock_s3)
            mock_db.upsert_file.assert_called_once()

    def test_poll_restores_no_restore_header(self):
        """File without Restore header should be skipped."""
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        mock_s3._config.get_s3_key.return_value = "file.zip"

        NOW = datetime.datetime.now(datetime.timezone.utc)
        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="file.zip",
            sha256_checksum="sha",
            size_bytes=0,
            tier="GLACIER",
            s3_etag="etag",
            last_sync_at=NOW,
            local_modified_at=NOW,
            remote_modified_at=NOW,
            restore_job_id="job",
        )
        mock_db.list_pending_restores.return_value = [record]
        mock_s3.head_object.return_value = {
            "StorageClass": "GLACIER",
            # No "Restore" key
        }

        with patch("sahara.notifier.notify_restore_complete") as mock_notify:
            poll_restores(mock_db, mock_s3)
            mock_notify.assert_not_called()
            mock_db.upsert_file.assert_not_called()


# ---------------------------------------------------------------------------
# poll_restore_expiries — additional coverage
# ---------------------------------------------------------------------------


class TestPollRestoreExpiriesExtended:
    def test_poll_expiries_no_expires_at(self):
        """Records without restore_expires_at should be skipped."""
        mock_db = MagicMock()
        NOW = datetime.datetime.now(datetime.timezone.utc)
        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="file.zip",
            sha256_checksum="sha",
            size_bytes=0,
            tier="HOT_TEMP",
            s3_etag="etag",
            last_sync_at=NOW,
            local_modified_at=NOW,
            remote_modified_at=NOW,
            restore_expires_at=None,  # No expiry
        )
        mock_db.list_expiring_restores.return_value = [record]

        with patch("sahara.notifier.notify_restore_expiring") as mock_notify:
            poll_restore_expiries(mock_db, within_hours=48)
            mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# uninstall_autostart — Windows path
# ---------------------------------------------------------------------------


class TestUninstallAutostartExtended:
    def test_uninstall_windows_calls_helper(self):
        with patch("sahara.daemon._uninstall_windows_startup") as mock_win:
            uninstall_autostart("Windows")
            mock_win.assert_called_once()

    def test_uninstall_unsupported_platform_does_nothing(self):
        # FreeBSD doesn't match any known platform
        uninstall_autostart("FreeBSD")  # Should not raise


# ---------------------------------------------------------------------------
# install_autostart — Windows path
# ---------------------------------------------------------------------------


class TestInstallAutostartWindows:
    def test_install_windows_raises_on_no_winreg(self):
        """On non-Windows, importing winreg raises ImportError, which triggers RuntimeError."""
        with patch("sahara.daemon._find_sahara_executable", return_value="/usr/bin/sahara"):
            # This should call _install_windows_startup which will fail to import winreg
            # on non-Windows systems
            try:
                result = install_autostart("Windows")
                # If somehow winreg exists (unlikely), just check we got a path back
                assert isinstance(result, str)
            except RuntimeError as e:
                assert "Failed to install Windows autostart" in str(e)
