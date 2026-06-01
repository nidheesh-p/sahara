"""Daemon management for Sahara — background sync process."""

from __future__ import annotations

import datetime
import logging
import os
import platform
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sahara.config import load_config

if TYPE_CHECKING:
    from sahara.storage.s3_client import S3Client
    from sahara.storage.state_db import StateDB

__all__ = [
    "start_daemon",
    "stop_daemon",
    "get_daemon_status",
    "pause_daemon",
    "resume_daemon",
    "is_daemon_running",
    "poll_restores",
    "poll_restore_expiries",
    "install_autostart",
    "uninstall_autostart",
]

logger = logging.getLogger(__name__)

_SAHARA_DIR = Path.home() / ".sahara"
_PID_FILE = _SAHARA_DIR / "daemon.pid"
_PAUSE_FILE = _SAHARA_DIR / "daemon.paused"
_LOG_FILE = _SAHARA_DIR / "daemon.log"

# Autostart platform constants
_LAUNCHD_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / "io.sahara.daemon.plist"
)
_SYSTEMD_SERVICE_PATH = (
    Path.home() / ".config" / "systemd" / "user" / "sahara.service"
)


# ---------------------------------------------------------------------------
# PID helpers
# ---------------------------------------------------------------------------


def _read_pid() -> int | None:
    """Read PID from the PID file; return None if absent or invalid."""
    pid_path = _PID_FILE
    if not pid_path.exists():
        return None
    try:
        text = pid_path.read_text().strip()
        return int(text)
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    _SAHARA_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(pid))


def _clear_pid() -> None:
    _PID_FILE.unlink(missing_ok=True)


def is_daemon_running() -> bool:
    """Return True if the PID file exists and the process is alive."""
    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = probe only
        return True
    except (ProcessLookupError, PermissionError):
        _clear_pid()
        return False


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------


def pause_daemon() -> None:
    """Pause sync activity by creating the pause sentinel file."""
    _SAHARA_DIR.mkdir(parents=True, exist_ok=True)
    _PAUSE_FILE.touch()
    logger.info("Daemon paused.")


def resume_daemon() -> None:
    """Resume sync activity by removing the pause sentinel file."""
    _PAUSE_FILE.unlink(missing_ok=True)
    logger.info("Daemon resumed.")


def _is_paused() -> bool:
    return _PAUSE_FILE.exists()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def get_daemon_status() -> dict:
    """Return a status dict describing the daemon's current state."""
    pid = _read_pid()
    running = is_daemon_running()
    return {
        "running": running,
        "pid": pid if running else None,
        "paused": _is_paused(),
        "pid_file": str(_PID_FILE),
        "log_file": str(_LOG_FILE),
    }


# ---------------------------------------------------------------------------
# Start daemon (fork)
# ---------------------------------------------------------------------------


def start_daemon(config_path: Path | None = None) -> None:
    """Fork and start the Sahara background sync daemon.

    The child process:
    1. Writes its PID to ~/.sahara/daemon.pid
    2. Redirects stdout/stderr to ~/.sahara/daemon.log
    3. Runs an initial sync
    4. Starts the file watcher + periodic poll loop
    """
    if is_daemon_running():
        raise RuntimeError("Daemon is already running.")

    if platform.system() == "Windows":
        # Windows does not support fork(); spawn a new process instead
        _start_daemon_windows(config_path)
        return

    pid = os.fork()
    if pid != 0:
        # Parent — return immediately
        logger.info("Daemon forked with PID %d", pid)
        return

    # Child process
    try:
        _daemon_main(config_path)
    except Exception as exc:
        logger.error("Daemon crashed: %s", exc)
    finally:
        _clear_pid()
        sys.exit(0)


def _start_daemon_windows(config_path: Path | None) -> None:
    """Start daemon on Windows using subprocess."""
    import subprocess

    args = [sys.executable, "-m", "sahara.daemon", "--daemon"]
    if config_path:
        args += ["--config", str(config_path)]

    proc = subprocess.Popen(
        args,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 8),
        close_fds=True,
    )
    logger.info("Daemon started (Windows) with PID %d", proc.pid)


def _daemon_main(config_path: Path | None) -> None:
    """Main loop for the daemon process."""
    import logging.handlers

    # Redirect output to log file
    _SAHARA_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        str(_LOG_FILE),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(fh)
    root_logger.setLevel(logging.INFO)

    _write_pid(os.getpid())

    # Detach from terminal
    try:
        os.setsid()
    except AttributeError:
        pass  # Windows

    config = load_config(config_path)

    from sahara.storage.s3_client import S3Client
    from sahara.storage.state_db import StateDB
    from sahara.sync.file_watcher import SaharaEventHandler, start_watching
    from sahara.sync.ignore_rules import IgnoreRules
    from sahara.sync.sync_engine import SyncEngine
    from sahara.utils.notifier import notify_sync_complete, notify_sync_error

    db = StateDB().connect()
    s3 = S3Client(config)

    def _make_engine_and_handler(
        folder: Path, s3_prefix: str = ""
    ) -> tuple[SyncEngine, SaharaEventHandler]:
        """Build a SyncEngine + SaharaEventHandler for a single folder."""
        ig = IgnoreRules(folder, extra_patterns=config.exclude_patterns)
        eng = SyncEngine(config, db, s3, ig, sync_folder=folder, s3_prefix=s3_prefix)

        def _on_changes(changed_paths: set[str]) -> None:
            if _is_paused():
                return
            logger.info(
                "Watcher triggered sync for %s (%d paths)", folder, len(changed_paths)
            )
            try:
                result = eng.sync()
                if result.had_errors:
                    notify_sync_error(len(result.failed))
                else:
                    notify_sync_complete(
                        len(result.uploaded),
                        len(result.downloaded),
                        len(result.deleted),
                        len(result.conflicts),
                    )
            except Exception as exc:
                logger.error("Sync failed in daemon for %s: %s", folder, exc)

        h = SaharaEventHandler(
            folder,
            on_changes=_on_changes,
            debounce_seconds=config.debounce_seconds,
            is_paused=_is_paused,
        )
        return eng, h

    # Build engines for primary folder + all registered additional targets
    primary_folder = config.get_sync_folder_path()
    engines: list[SyncEngine] = []
    watch_pairs: list[tuple[Path, SaharaEventHandler]] = []

    primary_engine, primary_handler = _make_engine_and_handler(primary_folder, "")
    engines.append(primary_engine)
    watch_pairs.append((primary_folder, primary_handler))

    for row in db.list_sync_targets():
        add_folder = Path(row["local_path"])
        if not add_folder.exists():
            logger.warning("Registered sync target missing: %s — skipping", add_folder)
            continue
        add_engine, add_handler = _make_engine_and_handler(add_folder, row["s3_prefix"])
        engines.append(add_engine)
        watch_pairs.append((add_folder, add_handler))

    observer = start_watching(watch_pairs)

    # Initial sync for all folders
    logger.info("Running initial sync for %d folder(s)…", len(engines))
    for eng in engines:
        try:
            result = eng.sync()
            logger.info("Initial sync complete for %s: %s", eng._sync_folder, result.summary_lines())
        except Exception as exc:
            logger.error("Initial sync failed for %s: %s", eng._sync_folder, exc)

    # Main poll loop
    def _handle_sigterm(*_: object) -> None:
        logger.info("SIGTERM received; shutting down daemon.")
        observer.stop()
        observer.join()
        db.close()
        _clear_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    last_poll = 0.0
    while True:
        time.sleep(5)
        now = time.monotonic()
        if now - last_poll >= config.poll_interval_seconds:
            last_poll = now
            if not _is_paused():
                try:
                    poll_restores(db, s3)
                except Exception as exc:
                    logger.error("Restore poll failed: %s", exc)
                try:
                    poll_restore_expiries(db)
                except Exception as exc:
                    logger.error("Expiry poll failed: %s", exc)


# ---------------------------------------------------------------------------
# Stop daemon
# ---------------------------------------------------------------------------


def stop_daemon() -> None:
    """Send SIGTERM to the running daemon."""
    pid = _read_pid()
    if pid is None:
        raise RuntimeError("No PID file found; daemon may not be running.")
    if not is_daemon_running():
        _clear_pid()
        raise RuntimeError("Daemon is not running (stale PID file cleaned up).")
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("Sent SIGTERM to daemon PID %d", pid)
    except ProcessLookupError as exc:
        _clear_pid()
        raise RuntimeError(f"Process {pid} not found.") from exc


# ---------------------------------------------------------------------------
# Restore polling
# ---------------------------------------------------------------------------


def poll_restores(db: StateDB, s3: S3Client) -> None:
    """Check all pending Glacier restores and update the DB."""
    from sahara.utils.notifier import notify_restore_complete

    pending = db.list_pending_restores()
    for record in pending:
        try:
            head = s3.head_object(s3._config.get_s3_key(record.relative_path))
            restore_header = head.get("Restore", "")
            if restore_header and 'ongoing-request="false"' in restore_header:
                import re

                m = re.search(r'expiry-date="([^"]+)"', restore_header)
                expiry = None
                if m:
                    from email.utils import parsedate_to_datetime

                    try:
                        expiry = parsedate_to_datetime(m.group(1))
                    except Exception:
                        pass

                record.restore_job_id = None
                record.tier = "HOT_TEMP"  # type: ignore[assignment]
                if expiry:
                    record.restore_expires_at = expiry
                db.upsert_file(record)
                notify_restore_complete(record.relative_path)
                logger.info("Restore complete for %s", record.relative_path)
        except Exception as exc:
            logger.warning(
                "Failed to check restore status for %s: %s",
                record.relative_path,
                exc,
            )


def poll_restore_expiries(db: StateDB, within_hours: int = 48) -> None:
    """Warn about restored files whose hot window is expiring soon."""
    from sahara.utils.notifier import notify_restore_expiring

    expiring = db.list_expiring_restores(within_hours=within_hours)
    now = datetime.datetime.now(datetime.UTC)
    for record in expiring:
        if record.restore_expires_at:
            delta = record.restore_expires_at - now
            hours_remaining = delta.total_seconds() / 3600
            if hours_remaining > 0:
                notify_restore_expiring(record.relative_path, hours_remaining)


# ---------------------------------------------------------------------------
# Platform autostart
# ---------------------------------------------------------------------------


def install_autostart(platform_name: str | None = None) -> str:
    """Install Sahara daemon to run at login.

    Returns the path of the created autostart file.
    """
    plat = platform_name or platform.system()
    sahara_bin = _find_sahara_executable()

    if plat == "Darwin":
        return _install_launchd(sahara_bin)
    elif plat == "Linux":
        return _install_systemd(sahara_bin)
    elif plat == "Windows":
        return _install_windows_startup(sahara_bin)
    else:
        raise RuntimeError(f"Autostart not supported on platform: {plat}")


def uninstall_autostart(platform_name: str | None = None) -> None:
    """Remove the autostart entry for the current platform."""
    plat = platform_name or platform.system()
    if plat == "Darwin":
        _LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
        logger.info("Removed launchd plist: %s", _LAUNCHD_PLIST_PATH)
    elif plat == "Linux":
        _SYSTEMD_SERVICE_PATH.unlink(missing_ok=True)
        logger.info("Removed systemd service: %s", _SYSTEMD_SERVICE_PATH)
    elif plat == "Windows":
        _uninstall_windows_startup()


def _find_sahara_executable() -> str:
    import shutil

    exe = shutil.which("sahara")
    if exe:
        return exe
    # Fallback: use python -m
    return f"{sys.executable} -m sahara"


def _install_launchd(sahara_bin: str) -> str:
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.sahara.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sahara_bin}</string>
        <string>daemon</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{_LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>{_LOG_FILE}</string>
</dict>
</plist>
"""
    _LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PLIST_PATH.write_text(plist_content)
    return str(_LAUNCHD_PLIST_PATH)


def _install_systemd(sahara_bin: str) -> str:
    service_content = f"""[Unit]
Description=Sahara personal cloud storage daemon
After=network.target

[Service]
Type=forking
ExecStart={sahara_bin} daemon start
ExecStop={sahara_bin} daemon stop
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    _SYSTEMD_SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_SERVICE_PATH.write_text(service_content)
    return str(_SYSTEMD_SERVICE_PATH)


def _install_windows_startup(sahara_bin: str) -> str:
    """Add Sahara to Windows startup via the registry."""
    try:
        import winreg  # type: ignore[import]

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "SaharaDaemon", 0, winreg.REG_SZ, sahara_bin + " daemon start")
        winreg.CloseKey(key)
        return r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run\SaharaDaemon"
    except Exception as exc:
        raise RuntimeError(f"Failed to install Windows autostart: {exc}") from exc


def _uninstall_windows_startup() -> None:
    try:
        import winreg  # type: ignore[import]

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, "SaharaDaemon")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception as exc:
        raise RuntimeError(f"Failed to remove Windows autostart: {exc}") from exc


# ---------------------------------------------------------------------------
# Entry point for Windows spawned daemon
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="sahara.daemon")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()

    if args.daemon:
        _daemon_main(args.config)
