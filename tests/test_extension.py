"""Tests for the Claude Desktop extension first-run flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.config import load_config
from sahara.extension import BackgroundIndexer, bootstrap_extension, clean_folder_args
from sahara.library import ensure_content_roots
from sahara.storage.state_db import StateDB


def _folder(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def test_clean_folder_args_drops_empty_values() -> None:
    assert clean_folder_args(["", "  ", "/a"]) == [Path("/a")]


def test_first_run_creates_config_and_registers_folders(tmp_path: Path) -> None:
    docs, notes_dir = _folder(tmp_path, "docs"), _folder(tmp_path, "notes")
    config_path, db_path = tmp_path / "config.toml", tmp_path / "state.db"

    config, notes = bootstrap_extension(
        [docs, notes_dir], config_path=config_path, db_path=db_path
    )

    assert notes == []
    assert config.sync_folder == str(docs.resolve())
    assert load_config(config_path).storage_mode == "none"
    db = StateDB(db_path).connect()
    try:
        paths = {root.local_path for root in ensure_content_roots(config, db)}
    finally:
        db.close()
    assert notes_dir.resolve() in paths


def test_second_run_is_idempotent_and_keeps_existing_config(tmp_path: Path) -> None:
    docs, extra = _folder(tmp_path, "docs"), _folder(tmp_path, "extra")
    config_path, db_path = tmp_path / "config.toml", tmp_path / "state.db"
    bootstrap_extension([docs], config_path=config_path, db_path=db_path)

    config, notes = bootstrap_extension(
        [extra, docs], config_path=config_path, db_path=db_path
    )

    assert notes == []
    assert config.sync_folder == str(docs.resolve())


def test_missing_folder_is_reported_not_fatal(tmp_path: Path) -> None:
    docs = _folder(tmp_path, "docs")

    _, notes = bootstrap_extension(
        [docs, tmp_path / "gone"],
        config_path=tmp_path / "config.toml",
        db_path=tmp_path / "state.db",
    )

    assert len(notes) == 1
    assert "not an existing folder" in notes[0]


def test_no_usable_folder_on_first_run_explains_next_step(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="extension settings"):
        bootstrap_extension(
            [], config_path=tmp_path / "config.toml", db_path=tmp_path / "state.db"
        )


def test_background_indexer_reports_ready(tmp_path: Path) -> None:
    docs = _folder(tmp_path, "docs")
    db_path = tmp_path / "state.db"
    config, _ = bootstrap_extension(
        [docs], config_path=tmp_path / "config.toml", db_path=db_path
    )

    with patch("sahara.library.IndexingService._index_root") as index_root:
        indexer = BackgroundIndexer(config, db_path)
        assert indexer.snapshot()["state"] == "idle"
        indexer.start()
        indexer.join(10)

    index_root.assert_called_once()
    assert indexer.snapshot() == {
        "state": "ready",
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
    }


def test_background_indexer_reports_failure(tmp_path: Path) -> None:
    docs = _folder(tmp_path, "docs")
    db_path = tmp_path / "state.db"
    config, _ = bootstrap_extension(
        [docs], config_path=tmp_path / "config.toml", db_path=db_path
    )

    with patch("sahara.library.IndexingService.index", side_effect=OSError("disk")):
        indexer = BackgroundIndexer(config, db_path)
        indexer.start()
        indexer.join(10)

    snapshot = indexer.snapshot()
    assert snapshot["state"] == "failed"
    assert "disk" in snapshot["error"]


def test_cli_extension_passes_folders_to_serve(tmp_path: Path) -> None:
    docs = _folder(tmp_path, "docs")
    with patch("sahara.mcp_server.serve") as serve:
        result = CliRunner().invoke(
            main,
            [
                "--config",
                str(tmp_path / "c.toml"),
                "mcp",
                "serve",
                "--extension",
                str(docs),
                "",
            ],
        )

    assert result.exit_code == 0, result.output
    assert serve.call_args.kwargs["extension_folders"] == [docs]


def test_cli_rejects_folders_without_extension_flag(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main, ["--config", str(tmp_path / "c.toml"), "mcp", "serve", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "--extension" in result.output


def test_index_status_tool_reports_background_progress(tmp_path: Path) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    from sahara.config import SaharaConfig
    from sahara.mcp_server import FreshnessCache, build_mcp_server

    class FakeIndexer:
        def snapshot(self) -> dict[str, object]:
            return {"state": "indexing"}

    server = build_mcp_server(
        config=SaharaConfig(sync_folder=str(tmp_path), storage_mode="none"),
        freshness_cache=FreshnessCache(lambda: {"stale": True}, clock=lambda: 0.0),
        background_indexer=FakeIndexer(),
    )
    tool = server._tool_manager.get_tool("sahara_index_status")
    assert tool is not None
    with patch("sahara.mcp_server.index_status", return_value={"indexed_files": 0}):
        status = tool.fn()

    assert status["background_index"] == {"state": "indexing"}
