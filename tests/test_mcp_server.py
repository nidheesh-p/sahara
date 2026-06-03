from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig
from sahara.mcp_server import index_status, list_folders, read_chunk, search_files
from sahara.storage.state_db import StateDB


def test_read_chunk_returns_indexed_chunk(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        chunk_id = db.upsert_chunk("", "notes.txt", 0, "hash", "hello from sahara")

        result = read_chunk(chunk_id, db=db)

        assert result is not None
        assert result["relative_path"] == "notes.txt"
        assert result["chunk_text"] == "hello from sahara"
    finally:
        db.close()


def test_index_status_reports_counts(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        db.upsert_embedding("", "notes.txt", "hash", "[0.1]", "hello")
        db.upsert_chunk("", "notes.txt", 0, "hash", "hello")

        result = index_status(db=db)

        assert result["indexed_files"] == 1
        assert result["indexed_chunks"] == 1
        assert result["latest_indexed_at"] is not None
        assert "vector_index_available" in result
    finally:
        db.close()


def test_list_folders_includes_primary_and_additional(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        db.add_sync_target(str(tmp_path / "work"), "work")
        config = SaharaConfig(sync_folder=str(tmp_path / "primary"))

        result = list_folders(config=config, db=db)

        assert result[0] == {
            "local_path": str(tmp_path / "primary"),
            "storage_prefix": "",
            "role": "primary",
        }
        assert result[1]["local_path"] == str(tmp_path / "work")
        assert result[1]["storage_prefix"] == "work"
        assert result[1]["role"] == "additional"
    finally:
        db.close()


def test_search_files_normalises_results(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        with patch("sahara.mcp_server.SearchEngine") as engine_cls:
            engine = engine_cls.return_value
            engine.search.return_value = [{
                "storage_prefix": "",
                "relative_path": "notes.txt",
                "score": 0.75,
                "snippet": "hello",
            }]

            result = search_files("hello", top_k=100, db=db)

        engine.search.assert_called_once_with("hello", top_k=20, storage_prefix=None)
        assert result == [{
            "storage_prefix": "",
            "relative_path": "notes.txt",
            "score": 0.75,
            "snippet": "hello",
        }]
    finally:
        db.close()


def test_mcp_serve_cli_invokes_server() -> None:
    runner = CliRunner()
    server = MagicMock()

    with patch("sahara.mcp_server.build_mcp_server", return_value=server) as build:
        result = runner.invoke(main, ["mcp", "serve"])

    assert result.exit_code == 0
    build.assert_called_once_with(config_path=None)
    server.run.assert_called_once_with()
