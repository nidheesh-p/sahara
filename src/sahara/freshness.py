"""Cheap index-staleness check: compare content roots on disk with the index inventory."""

from __future__ import annotations

import datetime
import os
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sahara.library import ContentRoot, stat_matches_entry
from sahara.scan_preview import MAX_ENTRIES, _inside
from sahara.storage.state_db import StateDB
from sahara.sync.ignore_rules import IgnoreRules

__all__ = ["IndexFreshness", "compute_freshness", "format_age", "registered_roots"]


@dataclass(frozen=True)
class IndexFreshness:
    last_indexed_at: str | None
    indexed_file_count: int
    pending_changes: int
    new_files: int
    modified_files: int
    missing_files: int
    is_lower_bound: bool
    watcher_running: bool
    stale: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _EntryLimitError(Exception):
    pass


def _walk(root: Path, ignore: IgnoreRules, budget: list[int]) -> Iterator[tuple[str, os.stat_result]]:
    """Yield (relative_path, stat) like ``summarize_folder`` walks, without reading contents.

    ``budget`` holds the remaining entry allowance shared across roots; raises
    ``_EntryLimitError`` once it is exhausted.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        relative_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if relative_dir == "." else relative_dir + "/"
        kept: list[str] = []
        for name in dirnames:
            budget[0] -= 1
            if budget[0] < 0:
                raise _EntryLimitError
            if not ignore.matches(prefix + name + "/"):
                kept.append(name)
        dirnames[:] = kept

        for filename in filenames:
            budget[0] -= 1
            if budget[0] < 0:
                raise _EntryLimitError
            relative_path = prefix + filename
            if ignore.matches(relative_path):
                continue
            file_path = os.path.join(dirpath, filename)
            if os.path.islink(file_path) and not _inside(root, file_path):
                continue
            try:
                stat = os.stat(file_path)
            except OSError:
                continue
            if not os.path.isfile(file_path):
                continue
            yield relative_path, stat


def registered_roots(db: StateDB) -> list[ContentRoot]:
    """Return content roots already registered in *db*, without migrating config.

    Unlike ``ensure_content_roots`` this never writes, so status checks stay read-only.
    """
    return [
        ContentRoot(
            local_path=Path(row["local_path"]),
            storage_prefix=row["storage_prefix"],
            is_primary=bool(row["is_primary"]),
            sync_enabled=bool(row["sync_enabled"]),
        )
        for row in db.list_content_roots()
    ]


def compute_freshness(
    db: StateDB,
    folders: Iterable[Any],
    watcher_running: bool,
    max_entries: int = MAX_ENTRIES,
    *,
    exclude_patterns: list[str] | None = None,
) -> IndexFreshness:
    """Count files changed since the last index across *folders* (content roots).

    A file is pending when it has no inventory row, or its size or mtime differ
    from the row ``IndexingService`` recorded; inventory rows whose file is gone
    count as pending too. Stops after *max_entries* directory entries, in which
    case ``pending_changes`` is a lower bound and missing files are not counted.
    """
    budget = [max_entries]
    new = modified = missing = 0
    indexed_count = 0
    latest: str | None = None
    lower_bound = False

    for folder in folders:
        entries = {
            row["relative_path"]: row
            for row in db.list_index_entries(storage_prefix=folder.storage_prefix)
        }
        for row in entries.values():
            if row["status"] == "indexed":
                indexed_count += 1
            if row["indexed_at"] and (latest is None or row["indexed_at"] > latest):
                latest = row["indexed_at"]
        if lower_bound:
            continue

        root = Path(os.path.realpath(folder.local_path))
        if not root.is_dir():
            continue
        ignore = IgnoreRules(root, extra_patterns=exclude_patterns)
        seen: set[str] = set()
        try:
            for relative_path, stat in _walk(root, ignore, budget):
                seen.add(relative_path)
                recorded = entries.get(relative_path)
                if recorded is None or recorded["status"] == "missing":
                    new += 1
                elif not stat_matches_entry(recorded, stat):
                    modified += 1
        except _EntryLimitError:
            lower_bound = True
            continue
        missing += sum(
            1
            for path, row in entries.items()
            if path not in seen and row["status"] != "missing"
        )

    pending = new + modified + missing
    return IndexFreshness(
        last_indexed_at=latest,
        indexed_file_count=indexed_count,
        pending_changes=pending,
        new_files=new,
        modified_files=modified,
        missing_files=missing,
        is_lower_bound=lower_bound,
        watcher_running=watcher_running,
        stale=pending > 0 and not watcher_running,
    )


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'} ago"


def format_age(
    timestamp: str | datetime.datetime | None,
    now: datetime.datetime,
) -> str:
    """Return e.g. ``3 minutes ago`` or ``never`` for a missing timestamp."""
    if timestamp is None:
        return "never"
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.datetime.fromisoformat(timestamp)
        except ValueError:
            return "unknown"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    seconds = int((now - timestamp).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return _plural(seconds // 60, "minute")
    if seconds < 86400:
        return _plural(seconds // 3600, "hour")
    return _plural(seconds // 86400, "day")
