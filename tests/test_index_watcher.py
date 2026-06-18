"""Tests for the always-local index watcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig, save_config
from sahara.index_watcher import LocalIndexWatcherService
from sahara.library import ContentRoot, IndexingService
from sahara.memory import MemoryService
from sahara.search.search_engine import IndexFileResult
from sahara.storage.state_db import StateDB


class RecordingIndexer:
    def __init__(self, roots: list[ContentRoot]) -> None:
        self._roots = roots
        self.indexed: list[tuple[Path, bool]] = []

    def roots(self) -> list[ContentRoot]:
        return self._roots

    def index_path(self, path: Path, *, force: bool = False) -> IndexFileResult:
        self.indexed.append((path, force))
        return IndexFileResult(indexed=True, reason="indexed")


def _config(tmp_path: Path, root: Path) -> SaharaConfig:
    return SaharaConfig(
        sync_folder=str(root),
        storage_mode="none",
        memory_folder=str(tmp_path / "memory"),
    )


def _root(path: Path, storage_prefix: str = "") -> ContentRoot:
    return ContentRoot(
        local_path=path,
        storage_prefix=storage_prefix,
        is_primary=storage_prefix == "",
        sync_enabled=False,
    )


def test_local_index_watcher_indexes_changed_file(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    note = content / "note.txt"
    note.write_text("remember this", encoding="utf-8")
    indexer = RecordingIndexer([_root(content)])

    with StateDB(tmp_path / "state.db") as db:
        service = LocalIndexWatcherService(
            _config(tmp_path, content),
            db,
            indexer=indexer,  # type: ignore[arg-type]
        )

        result = service.handle_path(note)

    assert result.action == "indexed"
    assert result.relative_path == "note.txt"
    assert indexer.indexed == [(note.resolve(), True)]


def test_local_index_watcher_removes_search_for_true_deletion(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    config = _config(tmp_path, content)
    indexer = RecordingIndexer([_root(content)])

    with StateDB(tmp_path / "state.db") as db:
        db.upsert_index_entry(
            "",
            "deleted.txt",
            content_hash="abc",
            size_bytes=10,
            modified_ns=1,
            status="indexed",
            reason="indexed",
        )
        with patch.object(db, "delete_search_index_for_file") as delete_search:
            service = LocalIndexWatcherService(
                config,
                db,
                indexer=indexer,  # type: ignore[arg-type]
            )

            result = service.handle_path(content / "deleted.txt")

        entries = db.list_index_entries(storage_prefix="")

    assert result.action == "deleted"
    assert entries[0]["status"] == "missing"
    delete_search.assert_called_once_with("", "deleted.txt")


def test_local_index_watcher_ignores_path_traversal_and_symlink_escape(
    tmp_path: Path,
) -> None:
    content = tmp_path / "content"
    content.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    indexer = RecordingIndexer([_root(content)])

    with StateDB(tmp_path / "state.db") as db:
        service = LocalIndexWatcherService(
            _config(tmp_path, content),
            db,
            indexer=indexer,  # type: ignore[arg-type]
        )

        traversal = service.handle_path(content / ".." / "outside.txt")
        symlink_result = None
        if hasattr(Path, "symlink_to"):
            link = content / "link.txt"
            link.symlink_to(outside)
            symlink_result = service.handle_path(link)

    assert traversal.action == "ignored"
    assert traversal.reason == "outside_content_roots"
    if symlink_result is not None:
        assert symlink_result.action == "ignored"
        assert symlink_result.reason == "outside_content_roots"
    assert indexer.indexed == []


def test_memory_inbox_normalizes_raw_files_and_deduplicates(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    config = _config(tmp_path, content)

    with StateDB(tmp_path / "state.db") as db:
        memory = MemoryService(config, db)
        inbox = memory.inbox_path()
        first = inbox / "first.md"
        second = inbox / "second.md"
        body = "# Vendor terms\n\nVendor X uses net-30 terms."
        first.write_text(body, encoding="utf-8")
        second.write_text(body, encoding="utf-8")
        service = LocalIndexWatcherService(config, db, memory=memory)

        with patch.object(
            IndexingService,
            "index_path",
            return_value=IndexFileResult(indexed=True, reason="indexed"),
        ):
            first_result = service.handle_path(first)
            second_result = service.handle_path(second)

        items = memory.list()

    assert first_result.action == "captured"
    assert second_result.action == "captured"
    assert not first.exists()
    assert not second.exists()
    assert len(items) == 1
    assert items[0].title == "Vendor terms"
    assert items[0].text == body
    assert items[0].tags == ("inbox",)


def test_remember_editor_helper_captures_text(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    config_path = tmp_path / "config.toml"
    save_config(_config(tmp_path, content), config_path)

    with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"), patch(
        "click.edit",
        return_value="Edited memory",
    ), patch.object(
        IndexingService,
        "index_path",
        return_value=IndexFileResult(indexed=True, reason="indexed"),
    ):
        result = CliRunner().invoke(
            main,
            ["--config", str(config_path), "remember", "--editor"],
        )

    assert result.exit_code == 0
    assert "Saved memory" in result.output


def test_remember_clipboard_helper_captures_text(tmp_path: Path) -> None:
    content = tmp_path / "content"
    content.mkdir()
    config_path = tmp_path / "config.toml"
    save_config(_config(tmp_path, content), config_path)

    with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"), patch(
        "sahara.cli._read_clipboard_text",
        return_value="Clipboard memory",
    ), patch.object(
        IndexingService,
        "index_path",
        return_value=IndexFileResult(indexed=True, reason="indexed"),
    ):
        result = CliRunner().invoke(
            main,
            ["--config", str(config_path), "remember", "--clipboard"],
        )

    assert result.exit_code == 0
    assert "Saved memory" in result.output
