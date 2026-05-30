"""Tests for sahara.daemon."""
from __future__ import annotations

import datetime
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sahara.daemon import (
    _clear_pid,
    _is_paused,
    _read_pid,
    _write_pid,
    get_daemon_status,
    install_autostart,
    is_daemon_running,
    pause_daemon,
    poll_restore_expiries,
    poll_restores,
    resume_daemon,
    stop_daemon,
    uninstall_autostart,
)

# ---------------------------------------------------------------------------
# PID helpers (patching the module-level paths)
# ---------------------------------------------------------------------------


class TestPidHelpers:
    def test_read_pid_returns_none_when_no_file(self, tmp_path: Path):
        with patch("sahara.daemon._PID_FILE", tmp_path / "nonexistent.pid"):
            result = _read_pid()
            assert result is None

    def test_read_pid_returns_int_when_file_exists(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345")
        with patch("sahara.daemon._PID_FILE", pid_file):
            result = _read_pid()
            assert result == 12345

    def test_read_pid_returns_none_for_invalid_content(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("not-a-number")
        with patch("sahara.daemon._PID_FILE", pid_file):
            result = _read_pid()
            assert result is None

    def test_write_pid(self, tmp_path: Path):
        with patch("sahara.daemon._SAHARA_DIR", tmp_path), \
             patch("sahara.daemon._PID_FILE", tmp_path / "daemon.pid"):
            _write_pid(99999)
            assert (tmp_path / "daemon.pid").read_text() == "99999"

    def test_clear_pid(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("12345")
        with patch("sahara.daemon._PID_FILE", pid_file):
            _clear_pid()
            assert not pid_file.exists()

    def test_clear_pid_nonexistent_is_safe(self, tmp_path: Path):
        with patch("sahara.daemon._PID_FILE", tmp_path / "nonexistent.pid"):
            _clear_pid()  # Should not raise


# ---------------------------------------------------------------------------
# is_daemon_running
# ---------------------------------------------------------------------------


class TestIsDaemonRunning:
    def test_returns_false_when_no_pid_file(self, tmp_path: Path):
        with patch("sahara.daemon._PID_FILE", tmp_path / "nonexistent.pid"):
            assert is_daemon_running() is False

    def test_returns_true_when_pid_alive(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text(str(os.getpid()))  # Current process is alive
        with patch("sahara.daemon._PID_FILE", pid_file):
            assert is_daemon_running() is True

    def test_returns_false_and_clears_pid_for_dead_process(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        # Use a very high PID that almost certainly doesn't exist
        pid_file.write_text("99999999")
        with patch("sahara.daemon._PID_FILE", pid_file):
            result = is_daemon_running()
            assert result is False
            assert not pid_file.exists()


# ---------------------------------------------------------------------------
# pause / resume / _is_paused
# ---------------------------------------------------------------------------


class TestPauseResume:
    def test_pause_creates_pause_file(self, tmp_path: Path):
        pause_file = tmp_path / "daemon.paused"
        with patch("sahara.daemon._SAHARA_DIR", tmp_path), \
             patch("sahara.daemon._PAUSE_FILE", pause_file):
            pause_daemon()
            assert pause_file.exists()

    def test_resume_removes_pause_file(self, tmp_path: Path):
        pause_file = tmp_path / "daemon.paused"
        pause_file.touch()
        with patch("sahara.daemon._PAUSE_FILE", pause_file):
            resume_daemon()
            assert not pause_file.exists()

    def test_is_paused_returns_true_when_file_exists(self, tmp_path: Path):
        pause_file = tmp_path / "daemon.paused"
        pause_file.touch()
        with patch("sahara.daemon._PAUSE_FILE", pause_file):
            assert _is_paused() is True

    def test_is_paused_returns_false_when_no_file(self, tmp_path: Path):
        with patch("sahara.daemon._PAUSE_FILE", tmp_path / "nonexistent.paused"):
            assert _is_paused() is False


# ---------------------------------------------------------------------------
# get_daemon_status
# ---------------------------------------------------------------------------


class TestGetDaemonStatus:
    def test_status_when_not_running(self, tmp_path: Path):
        with patch("sahara.daemon._PID_FILE", tmp_path / "daemon.pid"), \
             patch("sahara.daemon._PAUSE_FILE", tmp_path / "daemon.paused"), \
             patch("sahara.daemon._LOG_FILE", tmp_path / "daemon.log"):
            status = get_daemon_status()
            assert status["running"] is False
            assert status["pid"] is None
            assert status["paused"] is False

    def test_status_when_running(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text(str(os.getpid()))
        with patch("sahara.daemon._PID_FILE", pid_file), \
             patch("sahara.daemon._PAUSE_FILE", tmp_path / "daemon.paused"), \
             patch("sahara.daemon._LOG_FILE", tmp_path / "daemon.log"):
            status = get_daemon_status()
            assert status["running"] is True
            assert status["pid"] == os.getpid()

    def test_status_when_paused(self, tmp_path: Path):
        pause_file = tmp_path / "daemon.paused"
        pause_file.touch()
        with patch("sahara.daemon._PID_FILE", tmp_path / "daemon.pid"), \
             patch("sahara.daemon._PAUSE_FILE", pause_file), \
             patch("sahara.daemon._LOG_FILE", tmp_path / "daemon.log"):
            status = get_daemon_status()
            assert status["paused"] is True


# ---------------------------------------------------------------------------
# stop_daemon
# ---------------------------------------------------------------------------


class TestStopDaemon:
    def test_stop_raises_when_no_pid_file(self, tmp_path: Path):
        with patch("sahara.daemon._PID_FILE", tmp_path / "nonexistent.pid"):
            with pytest.raises(RuntimeError, match="No PID file"):
                stop_daemon()

    def test_stop_raises_when_not_running(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("99999999")  # Dead process
        with patch("sahara.daemon._PID_FILE", pid_file):
            with pytest.raises(RuntimeError):
                stop_daemon()

    def test_stop_sends_sigterm_to_running_process(self, tmp_path: Path):
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text(str(os.getpid()))
        with patch("sahara.daemon._PID_FILE", pid_file), \
             patch("os.kill") as mock_kill:
            # Patch is_daemon_running to return True
            with patch("sahara.daemon.is_daemon_running", return_value=True):
                stop_daemon()
                mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)


# ---------------------------------------------------------------------------
# poll_restores
# ---------------------------------------------------------------------------


class TestPollRestores:
    def test_poll_restores_updates_completed_restore(self):
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        mock_s3._config.get_s3_key.return_value = "archived.zip"

        now = datetime.datetime.now(datetime.UTC)

        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="archived.zip",
            sha256_checksum="sha",
            size_bytes=100,
            tier="GLACIER",
            s3_etag="etag",
            last_sync_at=now,
            local_modified_at=now,
            remote_modified_at=now,
            restore_job_id="job-123",
        )
        mock_db.list_pending_restores.return_value = [record]
        mock_s3.head_object.return_value = {
            "Restore": 'ongoing-request="false", expiry-date="Fri, 01 Jan 2027 00:00:00 GMT"',
            "StorageClass": "GLACIER",
        }

        with patch("sahara.notifier.notify_restore_complete"):
            poll_restores(mock_db, mock_s3)
            mock_db.upsert_file.assert_called_once()

    def test_poll_restores_handles_exception(self):
        mock_db = MagicMock()
        mock_s3 = MagicMock()

        now = datetime.datetime.now(datetime.UTC)
        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="file.zip",
            sha256_checksum="sha",
            size_bytes=0,
            tier="GLACIER",
            s3_etag="etag",
            last_sync_at=now,
            local_modified_at=now,
            remote_modified_at=now,
            restore_job_id="job",
        )
        mock_db.list_pending_restores.return_value = [record]
        mock_s3.head_object.side_effect = Exception("S3 error")

        # Should not raise
        poll_restores(mock_db, mock_s3)

    def test_poll_restores_skips_ongoing_restore(self):
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        mock_s3._config.get_s3_key.return_value = "archived.zip"

        now = datetime.datetime.now(datetime.UTC)
        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="archived.zip",
            sha256_checksum="sha",
            size_bytes=0,
            tier="GLACIER",
            s3_etag="etag",
            last_sync_at=now,
            local_modified_at=now,
            remote_modified_at=now,
            restore_job_id="job",
        )
        mock_db.list_pending_restores.return_value = [record]
        mock_s3.head_object.return_value = {
            "Restore": 'ongoing-request="true"',
            "StorageClass": "GLACIER",
        }

        poll_restores(mock_db, mock_s3)
        mock_db.upsert_file.assert_not_called()

    def test_poll_restores_empty_pending(self):
        mock_db = MagicMock()
        mock_s3 = MagicMock()
        mock_db.list_pending_restores.return_value = []
        poll_restores(mock_db, mock_s3)
        mock_s3.head_object.assert_not_called()


# ---------------------------------------------------------------------------
# poll_restore_expiries
# ---------------------------------------------------------------------------


class TestPollRestoreExpiries:
    def test_poll_expiries_notifies_expiring(self):
        mock_db = MagicMock()
        now = datetime.datetime.now(datetime.UTC)
        expires_soon = now + datetime.timedelta(hours=1)

        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="expiring.zip",
            sha256_checksum="sha",
            size_bytes=0,
            tier="HOT_TEMP",
            s3_etag="etag",
            last_sync_at=now,
            local_modified_at=now,
            remote_modified_at=now,
            restore_expires_at=expires_soon,
        )
        mock_db.list_expiring_restores.return_value = [record]

        with patch("sahara.notifier.notify_restore_expiring") as mock_notify:
            poll_restore_expiries(mock_db, within_hours=48)
            mock_notify.assert_called_once()
            call_args = mock_notify.call_args
            assert "expiring.zip" in str(call_args)

    def test_poll_expiries_skips_already_expired(self):
        mock_db = MagicMock()
        now = datetime.datetime.now(datetime.UTC)
        already_expired = now - datetime.timedelta(hours=1)

        from sahara.models import FileRecord
        record = FileRecord(
            relative_path="expired.zip",
            sha256_checksum="sha",
            size_bytes=0,
            tier="HOT_TEMP",
            s3_etag="etag",
            last_sync_at=now,
            local_modified_at=now,
            remote_modified_at=now,
            restore_expires_at=already_expired,
        )
        mock_db.list_expiring_restores.return_value = [record]

        with patch("sahara.notifier.notify_restore_expiring") as mock_notify:
            poll_restore_expiries(mock_db, within_hours=48)
            mock_notify.assert_not_called()

    def test_poll_expiries_empty(self):
        mock_db = MagicMock()
        mock_db.list_expiring_restores.return_value = []

        with patch("sahara.notifier.notify_restore_expiring") as mock_notify:
            poll_restore_expiries(mock_db)
            mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# install/uninstall autostart
# ---------------------------------------------------------------------------


class TestAutostart:
    def test_install_launchd(self, tmp_path: Path):
        plist_path = tmp_path / "io.sahara.daemon.plist"
        with patch("sahara.daemon._LAUNCHD_PLIST_PATH", plist_path), \
             patch("sahara.daemon._find_sahara_executable", return_value="/usr/local/bin/sahara"):
            result = install_autostart("Darwin")
            assert result == str(plist_path)
            assert plist_path.exists()
            content = plist_path.read_text()
            assert "io.sahara.daemon" in content

    def test_install_systemd(self, tmp_path: Path):
        service_path = tmp_path / "sahara.service"
        with patch("sahara.daemon._SYSTEMD_SERVICE_PATH", service_path), \
             patch("sahara.daemon._find_sahara_executable", return_value="/usr/bin/sahara"):
            result = install_autostart("Linux")
            assert result == str(service_path)
            assert service_path.exists()
            content = service_path.read_text()
            assert "sahara" in content.lower()

    def test_install_unsupported_platform_raises(self):
        with pytest.raises(RuntimeError, match="not supported"):
            install_autostart("FreeBSD")

    def test_uninstall_launchd(self, tmp_path: Path):
        plist_path = tmp_path / "io.sahara.daemon.plist"
        plist_path.touch()
        with patch("sahara.daemon._LAUNCHD_PLIST_PATH", plist_path):
            uninstall_autostart("Darwin")
            assert not plist_path.exists()

    def test_uninstall_systemd(self, tmp_path: Path):
        service_path = tmp_path / "sahara.service"
        service_path.touch()
        with patch("sahara.daemon._SYSTEMD_SERVICE_PATH", service_path):
            uninstall_autostart("Linux")
            assert not service_path.exists()

    def test_uninstall_nonexistent_is_safe(self, tmp_path: Path):
        plist_path = tmp_path / "nonexistent.plist"
        with patch("sahara.daemon._LAUNCHD_PLIST_PATH", plist_path):
            uninstall_autostart("Darwin")  # Should not raise

    def test_find_sahara_executable_uses_which(self):
        with patch("shutil.which", return_value="/usr/local/bin/sahara"):
            from sahara.daemon import _find_sahara_executable
            result = _find_sahara_executable()
            assert result == "/usr/local/bin/sahara"

    def test_find_sahara_executable_fallback(self):
        with patch("shutil.which", return_value=None):
            import sys

            from sahara.daemon import _find_sahara_executable
            result = _find_sahara_executable()
            assert sys.executable in result

    def test_start_daemon_already_running_raises(self):
        with patch("sahara.daemon.is_daemon_running", return_value=True):
            from sahara.daemon import start_daemon
            with pytest.raises(RuntimeError, match="already running"):
                start_daemon()
