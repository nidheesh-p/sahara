"""Desktop notification helpers for Sahara.

Uses plyer for cross-platform desktop notifications.
Gracefully degrades if plyer is not installed or the notification
subsystem is unavailable.
"""

from __future__ import annotations

import logging

__all__ = [
    "send_notification",
    "notify_restore_complete",
    "notify_restore_expiring",
    "notify_auth_failure",
    "notify_sync_error",
    "notify_sync_complete",
]

logger = logging.getLogger(__name__)

_APP_NAME = "Sahara"
_NOTIFY_TIMEOUT = 10  # seconds visible on desktop


def send_notification(
    title: str,
    message: str,
    app_name: str = _APP_NAME,
    timeout: int = _NOTIFY_TIMEOUT,
) -> None:
    """Send a desktop notification.

    Silently logs and continues if plyer is unavailable or the notification
    system cannot be reached (e.g. headless servers).
    """
    try:
        from plyer import notification as _notif  # type: ignore[import]

        _notif.notify(
            title=title,
            message=message,
            app_name=app_name,
            timeout=timeout,
        )
    except ImportError:
        logger.debug(
            "plyer not installed; desktop notification suppressed: [%s] %s",
            title,
            message,
        )
    except Exception as exc:
        # plyer raises various platform-specific exceptions
        logger.debug(
            "Desktop notification failed (non-fatal): %s — [%s] %s",
            exc,
            title,
            message,
        )


def notify_restore_complete(path: str) -> None:
    """Notify the user that a Glacier restore has completed."""
    send_notification(
        title=f"{_APP_NAME}: Restore Complete",
        message=f"Your file is ready to download:\n{path}",
    )


def notify_restore_expiring(path: str, hours_remaining: float) -> None:
    """Warn the user that a restored file will expire soon."""
    hours_int = int(hours_remaining)
    send_notification(
        title=f"{_APP_NAME}: Restore Expiring",
        message=(
            f"The restored copy of '{path}' will expire "
            f"in {hours_int} hour(s). Download it before it disappears."
        ),
    )


def notify_auth_failure() -> None:
    """Alert the user about an AWS authentication failure."""
    send_notification(
        title=f"{_APP_NAME}: Authentication Failure",
        message=(
            "Sahara could not authenticate with AWS. "
            "Run `sahara doctor` to diagnose."
        ),
    )


def notify_sync_error(error_count: int) -> None:
    """Notify the user that the sync cycle encountered errors."""
    send_notification(
        title=f"{_APP_NAME}: Sync Errors",
        message=(
            f"{error_count} file(s) failed to sync. "
            "Run `sahara status` for details."
        ),
    )


def notify_sync_complete(
    uploaded: int,
    downloaded: int,
    deleted: int,
    conflicts: int,
) -> None:
    """Notify the user that a sync cycle completed successfully."""
    if uploaded == 0 and downloaded == 0 and deleted == 0:
        return  # Nothing noteworthy to report

    parts: list[str] = []
    if uploaded:
        parts.append(f"{uploaded} uploaded")
    if downloaded:
        parts.append(f"{downloaded} downloaded")
    if deleted:
        parts.append(f"{deleted} deleted")
    if conflicts:
        parts.append(f"{conflicts} conflicts")

    summary = ", ".join(parts) if parts else "no changes"

    send_notification(
        title=f"{_APP_NAME}: Sync Complete",
        message=summary.capitalize() + ".",
    )
