"""Tests for sahara.file_watcher."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from sahara.file_watcher import Debouncer, SaharaEventHandler, start_watching

# ---------------------------------------------------------------------------
# Debouncer
# ---------------------------------------------------------------------------


class TestDebouncer:
    def test_touch_queues_path(self):
        fired_paths = []

        def callback(paths):
            fired_paths.extend(paths)

        d = Debouncer(callback, debounce_seconds=0.05)
        d.touch("file.txt")
        # Wait for the debouncer to fire
        time.sleep(0.3)
        d.stop()
        assert "file.txt" in fired_paths

    def test_multiple_touches_coalesced(self):
        fire_count = [0]
        fired_paths = []

        def callback(paths):
            fire_count[0] += 1
            fired_paths.extend(paths)

        d = Debouncer(callback, debounce_seconds=0.1)
        d.touch("file.txt")
        d.touch("file.txt")
        d.touch("file.txt")
        time.sleep(0.4)
        d.stop()
        assert fire_count[0] == 1
        assert "file.txt" in fired_paths

    def test_multiple_different_paths(self):
        fired_paths = []

        def callback(paths):
            fired_paths.extend(paths)

        d = Debouncer(callback, debounce_seconds=0.05)
        d.touch("a.txt")
        d.touch("b.txt")
        time.sleep(0.3)
        d.stop()
        assert "a.txt" in fired_paths
        assert "b.txt" in fired_paths

    def test_stop_prevents_further_firing(self):
        fire_count = [0]

        def callback(paths):
            fire_count[0] += 1

        d = Debouncer(callback, debounce_seconds=0.5)
        d.touch("file.txt")
        d.stop()
        time.sleep(0.6)
        # May or may not have fired once, but definitely not twice
        assert fire_count[0] <= 1

    def test_callback_exception_does_not_crash_debouncer(self):
        def bad_callback(paths):
            raise RuntimeError("callback error")

        d = Debouncer(bad_callback, debounce_seconds=0.05)
        d.touch("file.txt")
        time.sleep(0.3)
        d.stop()
        # Debouncer should have continued without crashing

    def test_ensure_running_restarts_dead_thread(self):
        fired = []

        def callback(paths):
            fired.extend(paths)

        d = Debouncer(callback, debounce_seconds=0.05)
        d.touch("first.txt")
        time.sleep(0.3)
        # Thread should have exited after processing
        d.touch("second.txt")
        time.sleep(0.3)
        d.stop()
        assert "first.txt" in fired
        assert "second.txt" in fired


# ---------------------------------------------------------------------------
# SaharaEventHandler
# ---------------------------------------------------------------------------


class TestSaharaEventHandler:
    def _make_handler(self, sync_folder: Path, callback=None):
        if callback is None:
            callback = MagicMock()
        return SaharaEventHandler(
            sync_folder=sync_folder,
            on_changes=callback,
            debounce_seconds=0.05,
        )

    def _make_event(self, event_class, src_path: str, is_directory: bool = False):
        event = MagicMock(spec=event_class)
        event.is_directory = is_directory
        event.src_path = src_path
        event.event_type = event_class.__name__
        return event

    def test_on_created_triggers_callback(self, tmp_path: Path):
        from watchdog.events import FileCreatedEvent
        changes = []
        handler = self._make_handler(tmp_path, callback=lambda p: changes.extend(p))
        event = self._make_event(FileCreatedEvent, str(tmp_path / "new_file.txt"))
        handler.on_created(event)
        time.sleep(0.3)
        handler.stop()
        assert "new_file.txt" in changes

    def test_on_modified_triggers_callback(self, tmp_path: Path):
        from watchdog.events import FileModifiedEvent
        changes = []
        handler = self._make_handler(tmp_path, callback=lambda p: changes.extend(p))
        event = self._make_event(FileModifiedEvent, str(tmp_path / "modified.txt"))
        handler.on_modified(event)
        time.sleep(0.3)
        handler.stop()
        assert "modified.txt" in changes

    def test_on_deleted_triggers_callback(self, tmp_path: Path):
        from watchdog.events import FileDeletedEvent
        changes = []
        handler = self._make_handler(tmp_path, callback=lambda p: changes.extend(p))
        event = self._make_event(FileDeletedEvent, str(tmp_path / "deleted.txt"))
        handler.on_deleted(event)
        time.sleep(0.3)
        handler.stop()
        assert "deleted.txt" in changes

    def test_directory_events_ignored(self, tmp_path: Path):
        from watchdog.events import FileCreatedEvent
        callback = MagicMock()
        handler = self._make_handler(tmp_path, callback=callback)
        event = self._make_event(FileCreatedEvent, str(tmp_path / "some_dir"), is_directory=True)
        handler.on_created(event)
        time.sleep(0.3)
        handler.stop()
        callback.assert_not_called()

    def test_sahara_internal_paths_ignored(self, tmp_path: Path):
        from watchdog.events import FileCreatedEvent
        callback = MagicMock()
        handler = self._make_handler(tmp_path, callback=callback)
        event = self._make_event(FileCreatedEvent, str(tmp_path / ".sahara" / "sync.lock"))
        handler.on_created(event)
        time.sleep(0.3)
        handler.stop()
        callback.assert_not_called()

    def test_paused_handler_ignores_events(self, tmp_path: Path):
        from watchdog.events import FileCreatedEvent
        callback = MagicMock()
        handler = SaharaEventHandler(
            sync_folder=tmp_path,
            on_changes=callback,
            debounce_seconds=0.05,
            is_paused=lambda: True,  # Always paused
        )
        event = self._make_event(FileCreatedEvent, str(tmp_path / "file.txt"))
        handler.on_created(event)
        time.sleep(0.3)
        handler.stop()
        callback.assert_not_called()

    def test_on_moved_triggers_both_paths(self, tmp_path: Path):
        from watchdog.events import FileMovedEvent
        changes = []
        handler = self._make_handler(tmp_path, callback=lambda p: changes.extend(p))

        event = MagicMock(spec=FileMovedEvent)
        event.is_directory = False
        event.src_path = str(tmp_path / "old.txt")
        event.dest_path = str(tmp_path / "new.txt")

        handler.on_moved(event)
        time.sleep(0.3)
        handler.stop()
        assert "old.txt" in changes
        assert "new.txt" in changes

    def test_on_moved_directory_ignored(self, tmp_path: Path):
        from watchdog.events import FileMovedEvent
        callback = MagicMock()
        handler = self._make_handler(tmp_path, callback=callback)

        event = MagicMock(spec=FileMovedEvent)
        event.is_directory = True
        event.src_path = str(tmp_path / "old_dir")
        event.dest_path = str(tmp_path / "new_dir")

        handler.on_moved(event)
        time.sleep(0.3)
        handler.stop()
        callback.assert_not_called()

    def test_rel_path_for_absolute_path(self, tmp_path: Path):
        handler = self._make_handler(tmp_path)
        rel = handler._rel(str(tmp_path / "subdir" / "file.txt"))
        assert rel == "subdir/file.txt"

    def test_rel_path_fallback_for_unrelated_path(self, tmp_path: Path):
        handler = self._make_handler(tmp_path)
        # Path not under sync_folder — returns as-is
        result = handler._rel("/completely/different/path.txt")
        assert "different" in result

    def test_stop_calls_debouncer_stop(self, tmp_path: Path):
        handler = self._make_handler(tmp_path)
        with patch.object(handler._debouncer, "stop") as mock_stop:
            handler.stop()
            mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# start_watching
# ---------------------------------------------------------------------------


class TestStartWatching:
    class FakeObserver:
        def __init__(self):
            self.scheduled = []
            self.started = False
            self.stopped = False
            self.joined = False

        def schedule(self, handler, path, recursive=True):
            self.scheduled.append((handler, path, recursive))

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started and not self.stopped

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            self.joined = True

    def test_start_watching_returns_observer(self, tmp_path: Path):
        handler = SaharaEventHandler(
            sync_folder=tmp_path,
            on_changes=MagicMock(),
            debounce_seconds=0.05,
        )
        observer = start_watching(
            [(tmp_path, handler)],
            recursive=True,
            observer_factory=self.FakeObserver,
        )
        assert observer is not None
        assert observer.is_alive()
        assert observer.scheduled == [(handler, str(tmp_path), True)]
        observer.stop()
        observer.join(timeout=5)
        assert observer.joined
        handler.stop()

    def test_start_watching_non_recursive(self, tmp_path: Path):
        handler = SaharaEventHandler(
            sync_folder=tmp_path,
            on_changes=MagicMock(),
            debounce_seconds=0.05,
        )
        observer = start_watching(
            [(tmp_path, handler)],
            recursive=False,
            observer_factory=self.FakeObserver,
        )
        assert observer is not None
        assert observer.scheduled == [(handler, str(tmp_path), False)]
        observer.stop()
        observer.join(timeout=5)
        handler.stop()
