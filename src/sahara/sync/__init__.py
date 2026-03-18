"""Sync layer — engine, file watcher, ignore rules, daemon.

Canonical import paths:
    from sahara.sync import SyncEngine, IgnoreRules, start_watching
    from sahara.sync.sync_engine import SyncEngine
    from sahara.sync.file_watcher import start_watching, SaharaEventHandler
    from sahara.sync.ignore_rules import IgnoreRules
    from sahara.sync.daemon import start_daemon, stop_daemon
"""

from sahara.sync.sync_engine import SyncEngine, DiffResult  # noqa: F401
from sahara.sync.file_watcher import SaharaEventHandler, Debouncer, start_watching  # noqa: F401
from sahara.sync.ignore_rules import IgnoreRules  # noqa: F401
from sahara.sync.daemon import (  # noqa: F401
    start_daemon, stop_daemon, get_daemon_status,
    pause_daemon, resume_daemon, is_daemon_running,
    poll_restores, poll_restore_expiries,
    install_autostart, uninstall_autostart,
)

__all__ = [
    "SyncEngine", "DiffResult",
    "SaharaEventHandler", "Debouncer", "start_watching",
    "IgnoreRules",
    "start_daemon", "stop_daemon", "get_daemon_status",
    "pause_daemon", "resume_daemon", "is_daemon_running",
    "poll_restores", "poll_restore_expiries",
    "install_autostart", "uninstall_autostart",
]
