"""File system watcher with debouncing for Sahara daemon mode."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
    FileSystemMovedEvent,
)
from watchdog.observers import Observer

__all__ = [
    "SaharaEventHandler",
    "Debouncer",
    "start_watching",
]

logger = logging.getLogger(__name__)


class ObserverProtocol(Protocol):
    """Observer behavior used by Sahara, independent of watchdog's platform alias."""

    def schedule(
        self,
        event_handler: FileSystemEventHandler,
        path: str,
        *,
        recursive: bool = False,
    ) -> object: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


# ---------------------------------------------------------------------------
# Debouncer
# ---------------------------------------------------------------------------


class Debouncer:
    """Coalesces rapid filesystem events per path.

    After *debounce_seconds* of inactivity the callback is fired once.
    """

    def __init__(
        self,
        callback: Callable[[set[str]], None],
        debounce_seconds: float = 2.0,
    ) -> None:
        self._callback = callback
        self._debounce = debounce_seconds
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def touch(self, path: str) -> None:
        """Record that *path* has changed."""
        with self._lock:
            self._pending[path] = time.monotonic()
        self._ensure_running()

    def _ensure_running(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop, daemon=True, name="sahara-debouncer"
            )
            self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(0.1)
            now = time.monotonic()
            ready: set[str] = set()
            with self._lock:
                for path, ts in list(self._pending.items()):
                    if now - ts >= self._debounce:
                        ready.add(path)
                for p in ready:
                    del self._pending[p]

            if ready:
                try:
                    self._callback(ready)
                except Exception as exc:
                    logger.error("Debounced callback raised: %s", exc)

            with self._lock:
                if not self._pending:
                    break

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------


class SaharaEventHandler(FileSystemEventHandler):
    """Watchdog event handler that feeds changed paths into the SyncEngine."""

    def __init__(
        self,
        sync_folder: Path,
        on_changes: Callable[[set[str]], None],
        debounce_seconds: float = 2.0,
        is_paused: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__()
        self._sync_folder = sync_folder
        self._is_paused = is_paused
        self._debouncer = Debouncer(
            callback=on_changes,
            debounce_seconds=debounce_seconds,
        )

    def _rel(self, abs_path: str) -> str:
        try:
            return Path(abs_path).relative_to(self._sync_folder).as_posix()
        except ValueError:
            return abs_path

    def _handle(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._is_paused():
            logger.debug("Daemon is paused; ignoring event: %s", event)
            return
        path = self._rel(str(event.src_path))
        # Skip .sahara internals
        if path.startswith(".sahara/"):
            return
        logger.debug("File event: %s %s", event.event_type, path)
        self._debouncer.touch(path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        if event.is_directory:
            return
        if self._is_paused():
            return
        src = self._rel(str(event.src_path))
        dst = self._rel(str(event.dest_path))
        if not src.startswith(".sahara/"):
            self._debouncer.touch(src)
        if not dst.startswith(".sahara/"):
            self._debouncer.touch(dst)

    def stop(self) -> None:
        self._debouncer.stop()


# ---------------------------------------------------------------------------
# Observer factory
# ---------------------------------------------------------------------------


def start_watching(
    folders: list[tuple[Path, SaharaEventHandler]],
    recursive: bool = True,
    observer_factory: Callable[[], ObserverProtocol] = Observer,
) -> ObserverProtocol:
    """Start a watchdog Observer for one or more folders.

    Args:
        folders: List of (sync_folder, event_handler) pairs to watch.
        recursive: Whether to watch subdirectories.

    Returns the running Observer; caller is responsible for calling
    observer.stop() and observer.join() on shutdown.
    """
    observer = observer_factory()
    for sync_folder, event_handler in folders:
        observer.schedule(event_handler, str(sync_folder), recursive=recursive)
        logger.info("File watcher registered on %s", sync_folder)
    observer.start()
    return observer
