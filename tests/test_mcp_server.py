from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig
from sahara.mcp_server import (
    StaticTokenVerifier,
    _require_compatible_mcp_sdk,
    ask_question,
    build_mcp_server,
    index_status,
    list_folders,
    read_chunk,
    search_files,
    serve,
)
from sahara.search.ask_engine import AskResult
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


def test_read_chunk_respects_allowlist_and_text_limit(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        chunk_id = db.upsert_chunk("work", "notes.txt", 0, "hash", "hello from sahara")

        result = read_chunk(
            chunk_id,
            allowed_storage_prefixes=("work",),
            max_snippet_chars=5,
            db=db,
        )

        assert result is not None
        assert result["chunk_text"] == "hello"

        with pytest.raises(ValueError, match="outside the MCP allowlist"):
            read_chunk(chunk_id, allowed_storage_prefixes=("personal",), db=db)
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
            "sync_enabled": False,
        }
        assert result[1]["local_path"] == str(tmp_path / "work")
        assert result[1]["storage_prefix"] == "work"
        assert result[1]["role"] == "additional"
        assert result[1]["sync_enabled"] is True
    finally:
        db.close()


def test_list_folders_respects_storage_prefix_allowlist(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        db.add_sync_target(str(tmp_path / "work"), "work")
        db.add_sync_target(str(tmp_path / "personal"), "personal")
        config = SaharaConfig(sync_folder=str(tmp_path / "primary"))

        result = list_folders(config=config, allowed_storage_prefixes=("work",), db=db)

        assert len(result) == 1
        assert result[0]["local_path"] == str(tmp_path / "work")
        assert result[0]["storage_prefix"] == "work"
        assert result[0]["role"] == "additional"
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
                "snippet": "hello from sahara",
            }]

            result = search_files("hello", top_k=100, max_snippet_chars=5, db=db)

        engine.search.assert_called_once_with("hello", top_k=20, storage_prefix=None)
        assert result == [{
            "storage_prefix": "",
            "relative_path": "notes.txt",
            "score": 0.75,
            "snippet": "hello",
            "local_state": "present",
        }]
    finally:
        db.close()


def test_search_files_rejects_disallowed_storage_prefix(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        with pytest.raises(ValueError, match="outside the MCP allowlist"):
            search_files(
                "hello",
                storage_prefix="personal",
                allowed_storage_prefixes=("work",),
                db=db,
            )
    finally:
        db.close()


def test_ask_question_uses_configured_provider(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    config = SaharaConfig(answer_provider="openai", answer_model="gpt-configured")
    try:
        with patch("sahara.mcp_server.AskEngine") as engine_cls:
            engine_cls.return_value.ask.return_value = AskResult(
                answer="answer",
                sources=[],
                model_used="gpt-configured",
                provider_used="openai",
            )

            payload = ask_question("question", config=config, db=db)

        engine_cls.assert_called_once()
        assert engine_cls.call_args.kwargs == {
            "provider": "openai",
            "model": "gpt-configured",
        }
        assert payload["provider_used"] == "openai"
    finally:
        db.close()


def test_ask_question_defaults_to_retrieval_only(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        with patch("sahara.mcp_server.AskEngine") as engine_cls:
            engine_cls.return_value.ask.return_value = AskResult(
                answer=None,
                sources=[],
            )

            payload = ask_question("question", config=SaharaConfig(), db=db)

        assert engine_cls.call_args.kwargs == {
            "provider": "none",
            "model": None,
        }
        assert payload["answer"] is None
        assert payload["degraded"] is False
        assert payload["error"] is None
    finally:
        db.close()


def test_static_token_verifier_accepts_only_configured_token() -> None:
    pytest.importorskip("mcp.server.auth.provider")
    verifier = StaticTokenVerifier("secret")

    valid = anyio.run(verifier.verify_token, "secret")
    invalid = anyio.run(verifier.verify_token, "wrong")

    assert valid is not None
    assert valid.scopes == ["sahara:read"]
    assert invalid is None


def test_compatible_mcp_sdk_rejects_version_without_token_verifier() -> None:
    class OldFastMCP:
        def __init__(self, name: str | None = None, **settings: object) -> None:
            pass

    with patch("sahara.mcp_server.version", return_value="1.9.0"):
        with pytest.raises(RuntimeError, match=r"MCP SDK 1\.14\.0 or newer") as exc_info:
            _require_compatible_mcp_sdk(OldFastMCP)

    assert "found 1.9.0" in str(exc_info.value)
    assert "pipx runpip sahara-memory" in str(exc_info.value)


def test_compatible_mcp_sdk_rejects_partially_compatible_version() -> None:
    class FastMCPWithTokenVerifier:
        def __init__(
            self,
            name: str | None = None,
            token_verifier: object | None = None,
            **settings: object,
        ) -> None:
            pass

    with patch("sahara.mcp_server.version", return_value="1.13.0"):
        with pytest.raises(RuntimeError, match="found 1.13.0"):
            _require_compatible_mcp_sdk(FastMCPWithTokenVerifier)


def test_compatible_mcp_sdk_accepts_minimum_version() -> None:
    class CompatibleFastMCP:
        def __init__(
            self,
            name: str | None = None,
            token_verifier: object | None = None,
            **settings: object,
        ) -> None:
            pass

    with patch("sahara.mcp_server.version", return_value="1.14.0"):
        _require_compatible_mcp_sdk(CompatibleFastMCP)


def test_build_mcp_server_accepts_static_token_with_supported_sdk() -> None:
    pytest.importorskip("mcp.server.fastmcp")

    server = build_mcp_server(auth_token="secret")

    assert server is not None


def test_mcp_serve_cli_invokes_server() -> None:
    runner = CliRunner()
    server = MagicMock()

    with patch("sahara.mcp_server.build_mcp_server", return_value=server) as build:
        result = runner.invoke(main, ["mcp", "serve"])

    assert result.exit_code == 0
    build.assert_called_once_with(
        config_path=None,
        host="127.0.0.1",
        port=8765,
        auth_token=None,
        allowed_tools=None,
        allowed_storage_prefixes=None,
        max_snippet_chars=500,
    )
    server.run.assert_called_once_with(transport="stdio")


def test_mcp_serve_cli_requires_auth_for_http_transport() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["mcp", "serve", "--transport", "http"])

    assert result.exit_code != 0
    assert "require --auth-token" in result.output


def test_mcp_serve_cli_reports_sdk_compatibility_error_without_traceback() -> None:
    runner = CliRunner()

    with patch(
        "sahara.mcp_server.serve",
        side_effect=RuntimeError("Authenticated HTTP MCP requires MCP SDK 1.14.0 or newer"),
    ):
        result = runner.invoke(
            main,
            ["mcp", "serve", "--transport", "http", "--auth-token", "secret"],
        )

    assert result.exit_code != 0
    assert "Error: Authenticated HTTP MCP requires MCP SDK 1.14.0 or newer" in result.output
    assert "Traceback" not in result.output


def test_mcp_serve_cli_accepts_http_transport_options() -> None:
    runner = CliRunner()
    server = MagicMock()

    with patch("sahara.mcp_server.build_mcp_server", return_value=server) as build:
        result = runner.invoke(
            main,
            [
                "mcp",
                "serve",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--auth-token",
                "secret",
                "--allow-tool",
                "sahara_search",
                "--allow-storage-prefix",
                "work",
                "--max-snippet-chars",
                "120",
            ],
        )

    assert result.exit_code == 0
    build.assert_called_once_with(
        config_path=None,
        host="127.0.0.1",
        port=8765,
        auth_token="secret",
        allowed_tools=("sahara_search",),
        allowed_storage_prefixes=("work",),
        max_snippet_chars=120,
    )
    server.run.assert_called_once_with(transport="streamable-http")


def test_mcp_serve_cli_warns_for_public_bind_and_insecure_http() -> None:
    runner = CliRunner()
    server = MagicMock()

    with patch("sahara.mcp_server.build_mcp_server", return_value=server):
        result = runner.invoke(
            main,
            [
                "mcp",
                "serve",
                "--transport",
                "http",
                "--host",
                "0.0.0.0",
                "--allow-insecure-http",
            ],
        )

    assert result.exit_code == 0
    assert "binding to 0.0.0.0:8765" in result.output
    assert "without bearer-token authentication" in result.output


def test_mcp_serve_function_passes_transport() -> None:
    server = MagicMock()

    with patch("sahara.mcp_server.build_mcp_server", return_value=server) as build:
        serve(
            config_path="config.toml",
            transport="streamable-http",
            host="0.0.0.0",
            port=9000,
            auth_token="secret",
            allowed_tools=("sahara_search",),
            allowed_storage_prefixes=("work",),
            max_snippet_chars=120,
        )

    build.assert_called_once_with(
        config_path="config.toml",
        host="0.0.0.0",
        port=9000,
        auth_token="secret",
        allowed_tools=("sahara_search",),
        allowed_storage_prefixes=("work",),
        max_snippet_chars=120,
    )
    server.run.assert_called_once_with(transport="streamable-http")
