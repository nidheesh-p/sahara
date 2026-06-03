"""Read-only MCP server for Sahara search and ask tools."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from sahara.config import DEFAULT_CONFIG_PATH, SaharaConfig, load_config
from sahara.search.ask_engine import AskEngine
from sahara.search.search_engine import SearchEngine
from sahara.storage.state_db import StateDB

__all__ = [
    "build_mcp_server",
    "read_chunk",
    "index_status",
    "list_folders",
    "search_files",
    "ask_question",
    "serve",
]


def _clamp_top_k(top_k: int) -> int:
    return max(1, min(int(top_k), 20))


def _normalise_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "storage_prefix": result.get("storage_prefix", ""),
        "relative_path": result.get("relative_path", ""),
        "score": float(result.get("score", 0.0)),
        "snippet": result.get("snippet", ""),
    }


def search_files(
    query: str,
    top_k: int = 5,
    storage_prefix: str | None = None,
    db: StateDB | None = None,
) -> list[dict[str, Any]]:
    """Return ranked indexed files/chunks for a query."""
    should_close = db is None
    db = db or StateDB().connect()
    try:
        engine = SearchEngine(db)
        return [
            _normalise_result(result)
            for result in engine.search(query, top_k=_clamp_top_k(top_k), storage_prefix=storage_prefix)
        ]
    finally:
        if should_close:
            db.close()


def ask_question(
    question: str,
    top_k: int = 5,
    storage_prefix: str | None = None,
    provider: str | None = None,
    db: StateDB | None = None,
) -> dict[str, Any]:
    """Answer a question over indexed files and return cited sources."""
    should_close = db is None
    db = db or StateDB().connect()
    try:
        search = SearchEngine(db)
        ask = AskEngine(search, provider=provider)
        result = ask.ask(question, top_k=_clamp_top_k(top_k), storage_prefix=storage_prefix)
        payload = asdict(result)
        payload["sources"] = [_normalise_result(source) for source in result.sources]
        return payload
    finally:
        if should_close:
            db.close()


def read_chunk(chunk_id: int, db: StateDB | None = None) -> dict[str, Any] | None:
    """Return a single indexed chunk by id."""
    should_close = db is None
    db = db or StateDB().connect()
    try:
        return db.get_chunk(int(chunk_id))
    finally:
        if should_close:
            db.close()


def list_folders(config: SaharaConfig | None = None, db: StateDB | None = None) -> list[dict[str, Any]]:
    """Return the primary sync folder plus additional registered folders."""
    config = config or load_config(DEFAULT_CONFIG_PATH)
    folders: list[dict[str, Any]] = []
    if config.sync_folder:
        folders.append({
            "local_path": config.sync_folder,
            "storage_prefix": "",
            "role": "primary",
        })

    should_close = db is None
    db = db or StateDB().connect()
    try:
        for target in db.list_sync_targets():
            folders.append({
                "local_path": target["local_path"],
                "storage_prefix": target["s3_prefix"],
                "role": "additional",
                "added_at": target["added_at"],
            })
        return folders
    finally:
        if should_close:
            db.close()


def index_status(db: StateDB | None = None) -> dict[str, Any]:
    """Return a compact summary of the local search index."""
    should_close = db is None
    db = db or StateDB().connect()
    try:
        return {
            "indexed_files": db.count_embeddings(),
            "indexed_chunks": db.count_chunks(),
            "latest_indexed_at": db.latest_chunk_indexed_at(),
            "vector_index_available": db.has_vec_table(),
            "embedding_model": "BAAI/bge-small-en-v1.5",
        }
    finally:
        if should_close:
            db.close()


def build_mcp_server(config_path: str | None = None) -> Any:
    """Create the Sahara FastMCP server.

    The import is lazy so normal Sahara installs do not require the optional
    `mcp` extra unless this server is used.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "The MCP server requires the optional mcp dependency. "
            "Install it with: pip install 'sahara[mcp]'"
        ) from exc

    config = load_config(Path(config_path) if config_path else DEFAULT_CONFIG_PATH)
    server = FastMCP("sahara")

    @server.tool()
    def sahara_search(
        query: str,
        top_k: int = 5,
        storage_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search Sahara's local index and return ranked cited snippets."""
        return search_files(query=query, top_k=top_k, storage_prefix=storage_prefix)

    @server.tool()
    def sahara_ask(
        question: str,
        top_k: int = 5,
        storage_prefix: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Ask a question over Sahara's local index and return cited sources."""
        return ask_question(
            question=question,
            top_k=top_k,
            storage_prefix=storage_prefix,
            provider=provider,
        )

    @server.tool()
    def sahara_read_chunk(chunk_id: int) -> dict[str, Any] | None:
        """Return one indexed chunk by Sahara chunk id."""
        return read_chunk(chunk_id)

    @server.tool()
    def sahara_list_folders() -> list[dict[str, Any]]:
        """List folders Sahara is configured to index or sync."""
        return list_folders(config=config)

    @server.tool()
    def sahara_index_status() -> dict[str, Any]:
        """Return local index counts and vector-index availability."""
        return index_status()

    return server


def serve(config_path: str | None = None) -> None:
    """Run Sahara's MCP server over stdio."""
    build_mcp_server(config_path=config_path).run()
