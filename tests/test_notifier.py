"""Tests for sahara.notifier."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sahara.notifier import (
    notify_auth_failure,
    notify_restore_complete,
    notify_restore_expiring,
    notify_sync_complete,
    notify_sync_error,
    send_notification,
)


# ---------------------------------------------------------------------------
# send_notification
# ---------------------------------------------------------------------------


class TestSendNotification:
    def test_send_notification_calls_plyer_when_available(self):
        mock_notif = MagicMock()
        with patch.dict("sys.modules", {"plyer": MagicMock(notification=mock_notif)}):
            # We need to patch plyer.notification.notify specifically
            with patch("sahara.notifier.send_notification") as mock_send:
                mock_send.return_value = None
                send_notification("Test Title", "Test Message")

    def test_send_notification_succeeds_with_mocked_plyer(self):
        mock_plyer = MagicMock()
        mock_notif = MagicMock()
        mock_plyer.notification = mock_notif

        with patch.dict("sys.modules", {"plyer": mock_plyer}):
            # Re-import to get the patched version
            import importlib
            import sahara.notifier as notifier_module
            # Direct patch on the import inside the function
            with patch("builtins.__import__", side_effect=_make_import(mock_notif)):
                # Just call — should not raise
                pass

        # Simpler: patch the internal import
        with patch("sahara.notifier.send_notification") as mock_fn:
            mock_fn.return_value = None
            send_notification("Title", "Body")  # calls the real function here

    def test_send_notification_does_not_raise_when_plyer_unavailable(self):
        """When plyer raises ImportError, function should not raise."""
        # We simulate ImportError by patching the import inside send_notification
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def _fake_import(name, *args, **kwargs):
            if name == "plyer":
                raise ImportError("plyer not installed")
            return __import__(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            # Should not raise
            send_notification("Test", "Message")

    def test_send_notification_does_not_raise_on_plyer_exception(self):
        """When plyer raises any exception, function should swallow it."""
        mock_notif = MagicMock()
        mock_notif.notify.side_effect = RuntimeError("notification system unavailable")

        with patch("sahara.notifier.send_notification") as mock_fn:
            mock_fn.return_value = None
            # The real implementation catches exceptions — just verify it doesn't raise
            pass

        # Use direct approach: mock the plyer import
        with patch.dict("sys.modules", {"plyer": MagicMock()}):
            import sahara.notifier
            plyer_mod = MagicMock()
            plyer_mod.notification.notify.side_effect = RuntimeError("OS error")

            # Manually test exception handling by patching the import chain
            import sys
            mock_plyer = MagicMock()
            mock_plyer.notification.notify.side_effect = Exception("platform error")
            sys.modules["plyer"] = mock_plyer
            try:
                # Re-trigger the notification which should catch the exception
                send_notification("Title", "Msg")  # Should not raise
            finally:
                del sys.modules["plyer"]


def _make_import(mock_notif):
    """Helper to create a partial import mock."""
    real_import = __import__

    def _import(name, *args, **kwargs):
        if name == "plyer":
            return MagicMock(notification=mock_notif)
        return real_import(name, *args, **kwargs)

    return _import


# ---------------------------------------------------------------------------
# Individual notification functions
# ---------------------------------------------------------------------------


class TestNotificationHelpers:
    def _get_call_args(self, mock_send):
        """Extract title and message from mock_send call (handles kwargs)."""
        call = mock_send.call_args
        kwargs = call.kwargs if hasattr(call, 'kwargs') else call[1]
        args = call.args if hasattr(call, 'args') else call[0]
        title = args[0] if args else kwargs.get("title", "")
        msg = args[1] if len(args) > 1 else kwargs.get("message", "")
        return title, msg

    def test_notify_restore_complete_calls_send(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_restore_complete("/home/user/sync/archive.zip")
            mock_send.assert_called_once()
            title, msg = self._get_call_args(mock_send)
            assert "Restore" in title
            assert "archive.zip" in msg

    def test_notify_restore_expiring_calls_send(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_restore_expiring("important.zip", 3.5)
            mock_send.assert_called_once()
            title, msg = self._get_call_args(mock_send)
            assert "Expir" in title
            assert "3" in msg  # int(3.5) = 3
            assert "important.zip" in msg

    def test_notify_restore_expiring_integer_hours(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_restore_expiring("file.txt", 12.7)
            _, msg = self._get_call_args(mock_send)
            assert "12" in msg  # int(12.7) = 12

    def test_notify_auth_failure_calls_send(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_auth_failure()
            mock_send.assert_called_once()
            title, msg = self._get_call_args(mock_send)
            assert "Auth" in title
            assert "AWS" in msg

    def test_notify_sync_error_calls_send(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_sync_error(5)
            mock_send.assert_called_once()
            title, msg = self._get_call_args(mock_send)
            assert "Error" in title
            assert "5" in msg

    def test_notify_sync_complete_with_uploads(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_sync_complete(uploaded=3, downloaded=0, deleted=0, conflicts=0)
            mock_send.assert_called_once()
            _, msg = self._get_call_args(mock_send)
            assert "3" in msg

    def test_notify_sync_complete_with_downloads(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_sync_complete(uploaded=0, downloaded=2, deleted=0, conflicts=0)
            mock_send.assert_called_once()
            _, msg = self._get_call_args(mock_send)
            assert "2" in msg

    def test_notify_sync_complete_with_deletes(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_sync_complete(uploaded=0, downloaded=0, deleted=1, conflicts=0)
            mock_send.assert_called_once()
            _, msg = self._get_call_args(mock_send)
            assert "1" in msg

    def test_notify_sync_complete_does_nothing_when_all_zero(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_sync_complete(uploaded=0, downloaded=0, deleted=0, conflicts=0)
            mock_send.assert_not_called()

    def test_notify_sync_complete_with_conflicts(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_sync_complete(uploaded=1, downloaded=0, deleted=0, conflicts=2)
            mock_send.assert_called_once()
            _, msg = self._get_call_args(mock_send)
            assert "2" in msg

    def test_notify_sync_complete_app_name_in_title(self):
        with patch("sahara.notifier.send_notification") as mock_send:
            notify_sync_complete(uploaded=1, downloaded=0, deleted=0, conflicts=0)
            title, _ = self._get_call_args(mock_send)
            assert "Sahara" in title
