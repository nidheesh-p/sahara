"""Always-local filesystem watching for incremental Sahara indexing."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent

from sahara.config import SaharaConfig
from sahara.library import ContentRoot, IndexingService
from sahara.memory import CaptureRequest, MemoryService
from sahara.memory.format import MAX_MEMORY_CHARS
from sahara.storage.state_db import StateDB
from sahara.sync.file_watcher import Debouncer, ObserverProtocol, start_watching
from sahara.sync.ignore_rules import IgnoreRules

__all__ = [
    "LocalIndexEventHandler",
    "LocalIndexResult",
    "LocalIndexWatcherService",
    "start_local_index_watching",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalIndexResult:
    """Result of handling one local watcher path."""

    action: str
    path: Path
    relative_path: str = ""
    reason: str = ""


class LocalIndexEventHandler(FileSystemEventHandler):
    """Watchdog handler that debounces absolute local paths for indexing."""

    def __init__(
        self,
        root: Path,
        on_paths: Callable[[set[Path]], object],
        *,
        debounce_seconds: float = 2.0,
        is_paused: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__()
        self._root = root.expanduser().resolve()
        self._is_paused = is_paused

        def _on_debounced(paths: set[str]) -> None:
            on_paths({Path(path) for path in paths})

        self._debouncer = Debouncer(
            callback=_on_debounced,
            debounce_seconds=debounce_seconds,
        )

    def _submit(self, raw_path: str) -> None:
        if self._is_paused():
            return
        self._debouncer.touch(str(Path(raw_path).expanduser()))

    def _handle(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._submit(str(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        if event.is_directory:
            return
        self._submit(str(event.src_path))
        self._submit(str(event.dest_path))

    def stop(self) -> None:
        self._debouncer.stop()


class LocalIndexWatcherService:
    """Handle local file events without requiring any storage backend."""

    def __init__(
        self,
        config: SaharaConfig,
        db: StateDB,
        *,
        indexer: IndexingService | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._indexer = indexer or IndexingService(config, db)
        self._memory = memory or MemoryService(config, db)
        self._inbox_path: Path | None = None
        self._inbox_error: str | None = None

    def watch_pairs(
        self,
        *,
        debounce_seconds: float | None = None,
        is_paused: Callable[[], bool] = lambda: False,
    ) -> list[tuple[Path, LocalIndexEventHandler]]:
        """Return content-root watch pairs for the always-local indexer."""
        self._ensure_inbox_path()
        pairs: list[tuple[Path, LocalIndexEventHandler]] = []
        for root in self._indexer.roots():
            if not root.local_path.is_dir():
                continue
            handler = LocalIndexEventHandler(
                root.local_path,
                self.handle_paths,
                debounce_seconds=(
                    self._config.debounce_seconds
                    if debounce_seconds is None
                    else debounce_seconds
                ),
                is_paused=is_paused,
            )
            pairs.append((root.local_path, handler))
        return pairs

    def handle_paths(self, paths: set[Path]) -> list[LocalIndexResult]:
        """Process a debounced batch of local watcher paths."""
        results: list[LocalIndexResult] = []
        for path in sorted(paths, key=lambda item: str(item)):
            try:
                results.append(self.handle_path(path))
            except Exception as exc:
                logger.error("Local index watcher failed for %s: %s", path, exc)
                results.append(LocalIndexResult("failed", path, reason=str(exc)))
        return results

    def handle_path(self, path: Path) -> LocalIndexResult:
        """Incrementally update local search state for one filesystem event."""
        inbox_result = self._handle_inbox_path(path)
        if inbox_result is not None:
            return inbox_result

        match = self._match_content_root(path)
        if match is None:
            return LocalIndexResult("ignored", path, reason="outside_content_roots")

        root, resolved, relative_path = match
        if self._is_control_path(relative_path):
            return LocalIndexResult("ignored", resolved, relative_path, "control_path")

        ignore = IgnoreRules(
            root.local_path,
            extra_patterns=self._config.exclude_patterns,
        )
        if ignore.matches(relative_path):
            return LocalIndexResult("ignored", resolved, relative_path, "ignored")

        if resolved.exists():
            if resolved.is_symlink() or not resolved.is_file():
                return LocalIndexResult(
                    "ignored",
                    resolved,
                    relative_path,
                    "not_regular_file",
                )
            indexed = self._indexer.index_path(resolved, force=True)
            return LocalIndexResult(
                "indexed",
                resolved,
                relative_path,
                indexed.reason,
            )

        self._mark_deleted(root, relative_path)
        return LocalIndexResult("deleted", resolved, relative_path, "not_found")

    def _handle_inbox_path(self, path: Path) -> LocalIndexResult | None:
        inbox = self._ensure_inbox_path()
        if inbox is None:
            return None
        resolved = self._safe_resolve(path, inbox)
        if resolved is None:
            return None
        try:
            relative_path = resolved.relative_to(inbox).as_posix()
        except ValueError:
            return None
        if not resolved.exists():
            return LocalIndexResult("ignored", resolved, relative_path, "not_found")
        if resolved.is_symlink() or not resolved.is_file():
            return LocalIndexResult(
                "ignored",
                resolved,
                relative_path,
                "not_regular_file",
            )

        raw = resolved.read_bytes()
        if len(raw) > MAX_MEMORY_CHARS:
            raise ValueError(
                f"Inbox memory exceeds the {MAX_MEMORY_CHARS:,}-character limit"
            )
        text = raw.decode("utf-8")
        if not text.strip():
            return LocalIndexResult("ignored", resolved, relative_path, "empty")
        digest = hashlib.sha256(raw).hexdigest()
        title = self._title_from_inbox_text(text, resolved)
        result = self._memory.capture(
            CaptureRequest(
                text=text,
                title=title,
                source_type="manual",
                source_id=f"inbox:{digest}",
                tags=("inbox",),
                idempotency_key=f"inbox:{digest}",
            )
        )
        resolved.unlink()
        return LocalIndexResult(
            "captured",
            result.item.path,
            result.item.relative_path,
            result.index_reason,
        )

    def _ensure_inbox_path(self) -> Path | None:
        if self._inbox_path is not None:
            return self._inbox_path
        if self._inbox_error is not None:
            return None
        try:
            self._inbox_path = self._memory.inbox_path()
        except Exception as exc:
            self._inbox_error = str(exc)
            logger.warning("Memory inbox is unavailable: %s", exc)
            return None
        return self._inbox_path

    def _match_content_root(
        self,
        path: Path,
    ) -> tuple[ContentRoot, Path, str] | None:
        roots = sorted(
            self._indexer.roots(),
            key=lambda root: len(root.local_path.parts),
            reverse=True,
        )
        for root in roots:
            resolved = self._safe_resolve(path, root.local_path)
            if resolved is None:
                continue
            try:
                relative_path = resolved.relative_to(
                    root.local_path.expanduser().resolve()
                ).as_posix()
            except ValueError:
                continue
            return root, resolved, relative_path
        return None

    def _mark_deleted(self, root: ContentRoot, relative_path: str) -> None:
        if not any(
            entry["relative_path"] == relative_path
            for entry in self._db.list_index_entries(
                storage_prefix=root.storage_prefix,
                limit=None,
            )
        ):
            return
        self._db.upsert_index_entry(
            root.storage_prefix,
            relative_path,
            content_hash=None,
            size_bytes=0,
            modified_ns=0,
            status="missing",
            reason="not_found",
        )
        residency = self._db.get_storage_residency(
            root.storage_prefix,
            relative_path,
        )
        if residency is not None:
            self._db.set_storage_residency(
                root.storage_prefix,
                relative_path,
                local_state="missing",
                remote_state=residency["remote_state"],
            )
        self._db.delete_search_index_for_file(root.storage_prefix, relative_path)

    @staticmethod
    def _safe_resolve(path: Path, root: Path) -> Path | None:
        resolved_root = root.expanduser().resolve()
        try:
            resolved = path.expanduser().resolve(strict=path.exists())
        except OSError:
            return None
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            return None
        return resolved

    @staticmethod
    def _is_control_path(relative_path: str) -> bool:
        return relative_path == ".sahara" or relative_path.startswith(".sahara/")

    @staticmethod
    def _title_from_inbox_text(text: str, path: Path) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                if title:
                    return title
            if stripped:
                return stripped[:80]
        return path.stem


def start_local_index_watching(
    config: SaharaConfig,
    db: StateDB,
    *,
    is_paused: Callable[[], bool] = lambda: False,
) -> tuple[ObserverProtocol, LocalIndexWatcherService]:
    """Start the always-local index watcher and return its observer/service."""
    service = LocalIndexWatcherService(config, db)
    observer = start_watching(
        service.watch_pairs(is_paused=is_paused),
    )
    return observer, service
