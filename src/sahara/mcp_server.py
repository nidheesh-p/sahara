"""Read-only MCP server for Sahara search and ask tools."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

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

McpTransport = Literal["stdio", "sse", "streamable-http"]
McpToolName = Literal[
    "sahara_search",
    "sahara_ask",
    "sahara_read_chunk",
    "sahara_list_folders",
    "sahara_index_status",
]
DEFAULT_MCP_TOOLS: tuple[McpToolName, ...] = (
    "sahara_search",
    "sahara_ask",
    "sahara_read_chunk",
    "sahara_list_folders",
    "sahara_index_status",
)


class StaticTokenVerifier:
    """Validate a single bearer token for remote MCP transports."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> Any | None:
        if not secrets.compare_digest(token, self._token):
            return None

        from mcp.server.auth.provider import AccessToken

        return AccessToken(token=token, client_id="sahara-remote-mcp", scopes=["sahara:read"])


def _clamp_top_k(top_k: int) -> int:
    return max(1, min(int(top_k), 20))


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return value[:max_chars]


def _normalise_result(result: dict[str, Any], *, max_snippet_chars: int = 500) -> dict[str, Any]:
    return {
        "storage_prefix": result.get("storage_prefix", ""),
        "relative_path": result.get("relative_path", ""),
        "score": float(result.get("score", 0.0)),
        "snippet": _truncate(result.get("snippet", ""), max_snippet_chars),
        "local_state": result.get("local_state", "present"),
    }


def _normalise_allowed_prefixes(allowed_storage_prefixes: tuple[str, ...] | None) -> set[str] | None:
    if not allowed_storage_prefixes:
        return None
    return {prefix.strip("/") for prefix in allowed_storage_prefixes}


def _validate_storage_prefix(
    storage_prefix: str | None,
    allowed_storage_prefixes: tuple[str, ...] | None,
) -> str | None:
    allowed = _normalise_allowed_prefixes(allowed_storage_prefixes)
    if not allowed:
        return storage_prefix

    requested = (storage_prefix or "").strip("/")
    if requested not in allowed:
        raise ValueError(
            "storage_prefix is outside the MCP allowlist; pass one of: "
            + ", ".join(sorted(allowed))
        )
    return requested


def search_files(
    query: str,
    top_k: int = 5,
    storage_prefix: str | None = None,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
    db: StateDB | None = None,
) -> list[dict[str, Any]]:
    """Return ranked indexed files/chunks for a query."""
    storage_prefix = _validate_storage_prefix(storage_prefix, allowed_storage_prefixes)
    should_close = db is None
    db = db or StateDB().connect()
    try:
        engine = SearchEngine(db)
        return [
            _normalise_result(result, max_snippet_chars=max_snippet_chars)
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
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
    db: StateDB | None = None,
) -> dict[str, Any]:
    """Answer a question over indexed files and return cited sources."""
    storage_prefix = _validate_storage_prefix(storage_prefix, allowed_storage_prefixes)
    should_close = db is None
    db = db or StateDB().connect()
    try:
        search = SearchEngine(db)
        ask = AskEngine(search, provider=provider)
        result = ask.ask(question, top_k=_clamp_top_k(top_k), storage_prefix=storage_prefix)
        payload = asdict(result)
        payload["sources"] = [
            _normalise_result(source, max_snippet_chars=max_snippet_chars) for source in result.sources
        ]
        return payload
    finally:
        if should_close:
            db.close()


def read_chunk(
    chunk_id: int,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
    db: StateDB | None = None,
) -> dict[str, Any] | None:
    """Return a single indexed chunk by id."""
    should_close = db is None
    db = db or StateDB().connect()
    try:
        chunk = db.get_chunk(int(chunk_id))
        if chunk is None:
            return None
        _validate_storage_prefix(chunk.get("storage_prefix", ""), allowed_storage_prefixes)
        chunk["chunk_text"] = _truncate(chunk.get("chunk_text", ""), max_snippet_chars)
        return chunk
    finally:
        if should_close:
            db.close()


def list_folders(
    config: SaharaConfig | None = None,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    db: StateDB | None = None,
) -> list[dict[str, Any]]:
    """Return folders Sahara indexes, including their sync state."""
    from sahara.library import ensure_content_roots

    config = config or load_config(DEFAULT_CONFIG_PATH)
    allowed = _normalise_allowed_prefixes(allowed_storage_prefixes)

    should_close = db is None
    db = db or StateDB().connect()
    try:
        folders: list[dict[str, Any]] = []
        for root in ensure_content_roots(config, db):
            prefix = root.storage_prefix
            if allowed is not None and prefix not in allowed:
                continue
            folders.append({
                "local_path": str(root.local_path),
                "storage_prefix": prefix,
                "role": "primary" if root.is_primary else "additional",
                "sync_enabled": root.sync_enabled,
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
            "offloaded_files": db.count_index_entries(status="offloaded"),
            "latest_indexed_at": db.latest_chunk_indexed_at(),
            "vector_index_available": db.has_vec_table(),
            "embedding_model": "BAAI/bge-small-en-v1.5",
        }
    finally:
        if should_close:
            db.close()


def build_mcp_server(
    config_path: str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    allowed_tools: tuple[McpToolName, ...] | None = DEFAULT_MCP_TOOLS,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
) -> Any:
    """Create the Sahara FastMCP server.

    The import is lazy so normal Sahara installs do not require the optional
    `mcp` extra unless this server is used.
    """
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.server import AuthSettings
    except ImportError as exc:
        raise RuntimeError(
            "The MCP server requires the optional mcp dependency. "
            "Install it with: pip install 'sahara-memory[mcp]'"
        ) from exc

    config = load_config(Path(config_path) if config_path else DEFAULT_CONFIG_PATH)
    auth = None
    token_verifier = None
    if auth_token:
        issuer_url: Any = f"http://127.0.0.1:{port}"
        auth = AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=issuer_url,
            required_scopes=["sahara:read"],
        )
        token_verifier = StaticTokenVerifier(auth_token)

    server = FastMCP("sahara", host=host, port=port, auth=auth, token_verifier=token_verifier)
    tool_allowlist = set(allowed_tools or DEFAULT_MCP_TOOLS)

    if "sahara_search" in tool_allowlist:
        @server.tool()
        def sahara_search(
            query: str,
            top_k: int = 5,
            storage_prefix: str | None = None,
        ) -> list[dict[str, Any]]:
            """Search Sahara's local index and return ranked cited snippets."""
            return search_files(
                query=query,
                top_k=top_k,
                storage_prefix=storage_prefix,
                allowed_storage_prefixes=allowed_storage_prefixes,
                max_snippet_chars=max_snippet_chars,
            )

    if "sahara_ask" in tool_allowlist:
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
                allowed_storage_prefixes=allowed_storage_prefixes,
                max_snippet_chars=max_snippet_chars,
            )

    if "sahara_read_chunk" in tool_allowlist:
        @server.tool()
        def sahara_read_chunk(chunk_id: int) -> dict[str, Any] | None:
            """Return one indexed chunk by Sahara chunk id."""
            return read_chunk(
                chunk_id,
                allowed_storage_prefixes=allowed_storage_prefixes,
                max_snippet_chars=max_snippet_chars,
            )

    if "sahara_list_folders" in tool_allowlist:
        @server.tool()
        def sahara_list_folders() -> list[dict[str, Any]]:
            """List folders Sahara is configured to index or sync."""
            return list_folders(config=config, allowed_storage_prefixes=allowed_storage_prefixes)

    if "sahara_index_status" in tool_allowlist:
        @server.tool()
        def sahara_index_status() -> dict[str, Any]:
            """Return local index counts and vector-index availability."""
            return index_status()

    return server


def serve(
    config_path: str | None = None,
    *,
    transport: McpTransport = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    allowed_tools: tuple[McpToolName, ...] | None = DEFAULT_MCP_TOOLS,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
) -> None:
    """Run Sahara's MCP server over the requested transport."""
    build_mcp_server(
        config_path=config_path,
        host=host,
        port=port,
        auth_token=auth_token,
        allowed_tools=allowed_tools,
        allowed_storage_prefixes=allowed_storage_prefixes,
        max_snippet_chars=max_snippet_chars,
    ).run(transport=transport)
