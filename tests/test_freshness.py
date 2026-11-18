from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest

from sahara.freshness import compute_freshness, format_age
from sahara.library import ContentRoot
from sahara.storage.state_db import StateDB

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.UTC)


def _root(path: Path, prefix: str = "") -> ContentRoot:
    path.mkdir(parents=True, exist_ok=True)
    return ContentRoot(path, prefix, is_primary=prefix == "", sync_enabled=False)


def _record(db: StateDB, root: ContentRoot, relative_path: str, indexed_at: str) -> None:
    """Record a file the way IndexingService does after indexing it."""
    stat = (root.local_path / relative_path).stat()
    db.upsert_index_entry(
        root.storage_prefix,
        relative_path,
        content_hash="hash",
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        status="indexed",
        indexed_at=indexed_at,
    )


@pytest.fixture
def db(tmp_path: Path):
    with StateDB(tmp_path / "state.db") as database:
        yield database


def test_detects_added_edited_and_deleted_files(tmp_path: Path, db: StateDB) -> None:
    root = _root(tmp_path / "notes")
    for name in ("same.md", "edited.md", "deleted.md"):
        (root.local_path / name).write_text("original")
        _record(db, root, name, "2026-11-18T11:00:00+00:00")
    db.upsert_index_entry(
        "", "same.md", content_hash="hash",
        size_bytes=8, modified_ns=(root.local_path / "same.md").stat().st_mtime_ns,
        status="indexed", indexed_at="2026-11-18T11:30:00+00:00",
    )
    (root.local_path / "edited.md").write_text("changed contents")
    (root.local_path / "deleted.md").unlink()
    (root.local_path / "added.md").write_text("new")

    freshness = compute_freshness(db, [root], watcher_running=False)

    assert (freshness.new_files, freshness.modified_files, freshness.missing_files) == (1, 1, 1)
    assert freshness.pending_changes == 3
    assert freshness.indexed_file_count == 3
    assert freshness.last_indexed_at == "2026-11-18T11:30:00+00:00"
    assert freshness.is_lower_bound is False
    assert freshness.stale is True


def test_mtime_change_alone_is_pending(tmp_path: Path, db: StateDB) -> None:
    root = _root(tmp_path / "notes")
    path = root.local_path / "touched.md"
    path.write_text("same")
    _record(db, root, "touched.md", "2026-11-18T11:00:00+00:00")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    assert compute_freshness(db, [root], False).modified_files == 1


def test_unchanged_index_is_fresh(tmp_path: Path, db: StateDB) -> None:
    root = _root(tmp_path / "notes")
    (root.local_path / "a.md").write_text("a")
    _record(db, root, "a.md", "2026-11-18T11:00:00+00:00")

    freshness = compute_freshness(db, [root], watcher_running=False)

    assert freshness.pending_changes == 0
    assert freshness.stale is False


def test_running_watcher_is_not_stale(tmp_path: Path, db: StateDB) -> None:
    root = _root(tmp_path / "notes")
    (root.local_path / "a.md").write_text("a")

    freshness = compute_freshness(db, [root], watcher_running=True)

    assert freshness.pending_changes == 1
    assert freshness.stale is False


def test_ignored_files_and_other_roots_are_skipped(tmp_path: Path, db: StateDB) -> None:
    root = _root(tmp_path / "notes")
    other = _root(tmp_path / "work", "work")
    (root.local_path / "scratch.tmp").write_text("x")
    (other.local_path / "a.md").write_text("a")
    _record(db, other, "a.md", "2026-11-18T11:00:00+00:00")

    freshness = compute_freshness(db, [root, other], False, exclude_patterns=["*.tmp"])

    assert freshness.pending_changes == 0
    assert freshness.indexed_file_count == 1


def test_empty_index_has_no_timestamp(tmp_path: Path, db: StateDB) -> None:
    freshness = compute_freshness(db, [_root(tmp_path / "notes")], False)

    assert freshness.last_indexed_at is None
    assert freshness.to_dict() == {
        "last_indexed_at": None,
        "indexed_file_count": 0,
        "pending_changes": 0,
        "new_files": 0,
        "modified_files": 0,
        "missing_files": 0,
        "is_lower_bound": False,
        "watcher_running": False,
        "stale": False,
    }


def test_entry_cap_reports_lower_bound_without_missing(tmp_path: Path, db: StateDB) -> None:
    root = _root(tmp_path / "notes")
    (root.local_path / "gone.md").write_text("x")
    _record(db, root, "gone.md", "2026-11-18T11:00:00+00:00")
    (root.local_path / "gone.md").unlink()
    for index in range(5):
        (root.local_path / f"new{index}.md").write_text("x")

    freshness = compute_freshness(db, [root], False, max_entries=2)

    assert freshness.is_lower_bound is True
    assert freshness.new_files == 2
    assert freshness.missing_files == 0
    assert freshness.stale is True


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (None, "never"),
        ("2026-11-18T11:59:30+00:00", "just now"),
        ("2026-11-18T11:59:00+00:00", "1 minute ago"),
        ("2026-11-18T11:57:00+00:00", "3 minutes ago"),
        (datetime.datetime(2026, 11, 18, 7, 0, tzinfo=datetime.UTC), "5 hours ago"),
        ("2026-11-16T12:00:00", "2 days ago"),
        ("not a timestamp", "unknown"),
    ],
)
def test_format_age(timestamp: object, expected: str) -> None:
    assert format_age(timestamp, NOW) == expected
