"""MCP server for Sahara retrieval and explicitly enabled local capture."""

from __future__ import annotations

import hashlib
import inspect
import re
import secrets
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from sahara.config import DEFAULT_CONFIG_PATH, SaharaConfig, load_config
from sahara.memory import CaptureRequest, MemoryFilters, MemoryService
from sahara.memory.format import SOURCE_TYPES
from sahara.search.ask_engine import AskEngine
from sahara.search.search_engine import SearchEngine
from sahara.storage.state_db import StateDB

__all__ = [
    "build_mcp_server",
    "read_chunk",
    "index_status",
    "list_folders",
    "recall_memories",
    "remember_memory",
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
    "sahara_recall",
    "sahara_remember",
]
DEFAULT_MCP_TOOLS: tuple[McpToolName, ...] = (
    "sahara_search",
    "sahara_ask",
    "sahara_read_chunk",
    "sahara_list_folders",
    "sahara_index_status",
    "sahara_recall",
)
MCP_MEMORY_MAX_CHARS = 20_000


def _require_compatible_mcp_sdk(fast_mcp_class: type[Any]) -> None:
    """Reject MCP SDK releases that cannot serve Sahara's authenticated tools."""
    try:
        installed_version = version("mcp")
    except PackageNotFoundError:
        installed_version = "unknown"

    version_match = re.match(r"^(\d+)\.(\d+)", installed_version)
    version_supported = (
        version_match is not None
        and (int(version_match.group(1)), int(version_match.group(2))) >= (1, 14)
    )
    token_verifier_supported = "token_verifier" in inspect.signature(fast_mcp_class).parameters
    if version_supported and token_verifier_supported:
        return

    raise RuntimeError(
        "Authenticated HTTP MCP requires MCP SDK 1.14.0 or newer "
        f"(found {installed_version}). Upgrade with "
        "`pipx runpip sahara-memory install --upgrade 'mcp>=1.14.0'`, "
        "or run `python -m pip install --upgrade 'mcp>=1.14.0'` in the environment "
        "that runs Sahara."
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
    config: SaharaConfig | None = None,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
    db: StateDB | None = None,
) -> dict[str, Any]:
    """Answer a question over indexed files and return cited sources."""
    storage_prefix = _validate_storage_prefix(storage_prefix, allowed_storage_prefixes)
    config = config or load_config(DEFAULT_CONFIG_PATH)
    selected_provider = provider or config.answer_provider
    selected_model = (
        config.answer_model
        if selected_provider == config.answer_provider and config.answer_model
        else None
    )
    should_close = db is None
    db = db or StateDB().connect()
    try:
        search = SearchEngine(db)
        ask = AskEngine(search, provider=selected_provider, model=selected_model)
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


def _ensure_memory_scope_allowed(
    service: MemoryService,
    db: StateDB,
    allowed_storage_prefixes: tuple[str, ...] | None,
) -> None:
    if not allowed_storage_prefixes:
        return
    service.list()
    root = db.get_content_root(str(service.root))
    if root is None:
        raise ValueError("Managed Sahara memory root is not registered")
    _validate_storage_prefix(
        root["storage_prefix"],
        allowed_storage_prefixes,
    )


def recall_memories(
    query: str,
    top_k: int = 5,
    source_types: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    since: str | None = None,
    until: str | None = None,
    *,
    config: SaharaConfig | None = None,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
    db: StateDB | None = None,
) -> list[dict[str, Any]]:
    """Recall only managed captured memories with body citations."""
    config = config or load_config(DEFAULT_CONFIG_PATH)
    should_close = db is None
    db = db or StateDB().connect()
    try:
        service = MemoryService(config, db)
        _ensure_memory_scope_allowed(
            service,
            db,
            allowed_storage_prefixes,
        )
        results = service.search(
            query,
            MemoryFilters(
                source_types=source_types,
                tags=tags,
                since=since,
                until=until,
            ),
            top_k=_clamp_top_k(top_k),
        )
        return [
            {
                "memory_id": result.item.memory_id,
                "title": result.item.title,
                "score": result.score,
                "snippet": _truncate(
                    result.snippet,
                    max_snippet_chars,
                ),
                "relative_path": result.item.relative_path,
                "source_type": result.item.source_type,
                "source_url": result.item.source_url,
                "tags": list(result.item.tags),
                "updated_at": result.item.updated_at,
            }
            for result in results
        ]
    finally:
        if should_close:
            db.close()


def remember_memory(
    text: str,
    *,
    idempotency_key: str,
    explicit_user_request: bool,
    title: str | None = None,
    source_type: str = "ai-chat",
    source_url: str = "",
    source_id: str = "",
    tags: tuple[str, ...] = (),
    config: SaharaConfig | None = None,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    db: StateDB | None = None,
) -> dict[str, Any]:
    """Create one memory after explicit user authorization and audit it."""
    config = config or load_config(DEFAULT_CONFIG_PATH)
    should_close = db is None
    db = db or StateDB().connect()
    try:
        key_hash = hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()
        audited_source_type = (
            source_type if source_type in SOURCE_TYPES else "invalid"
        )
        audit_id = db.begin_mcp_memory_audit(
            source_type=audited_source_type,
            idempotency_key_hash=key_hash,
        )

        def reject(reason: str, message: str) -> None:
            db.finish_mcp_memory_audit(
                audit_id,
                outcome="rejected",
                details=reason,
            )
            raise ValueError(message)

        if not explicit_user_request:
            reject(
                "explicit_user_request_required",
                "Memory capture requires an explicit user request to save this information",
            )
        if not idempotency_key.strip():
            reject(
                "idempotency_key_required",
                "Memory capture requires a non-empty idempotency key",
            )
        if len(text) > MCP_MEMORY_MAX_CHARS:
            reject(
                "request_too_large",
                f"MCP memory text exceeds the {MCP_MEMORY_MAX_CHARS:,}-character limit",
            )
        if source_type not in SOURCE_TYPES:
            reject(
                "invalid_source_type",
                "Unsupported memory source type",
            )

        service = MemoryService(config, db)
        try:
            _ensure_memory_scope_allowed(
                service,
                db,
                allowed_storage_prefixes,
            )
            result = service.capture(
                CaptureRequest(
                    text=text,
                    title=title,
                    source_type=source_type,
                    source_url=source_url,
                    source_id=source_id,
                    tags=tags,
                    idempotency_key=idempotency_key,
                )
            )
        except ValueError:
            db.finish_mcp_memory_audit(
                audit_id,
                outcome="rejected",
                details="validation_error",
            )
            raise
        except Exception:
            db.finish_mcp_memory_audit(
                audit_id,
                outcome="failed",
                details="capture_error",
            )
            raise

        if result.deduplicated:
            status = "already_saved"
        elif result.indexed:
            status = "saved_and_indexed"
        else:
            status = "saved_index_pending"
        db.finish_mcp_memory_audit(
            audit_id,
            outcome=status,
            memory_id=result.item.memory_id,
        )
        return {
            "status": status,
            "memory_id": result.item.memory_id,
            "title": result.item.title,
            "relative_path": result.item.relative_path,
            "indexed": result.indexed,
            "index_reason": result.index_reason,
            "deduplicated": result.deduplicated,
        }
    finally:
        if should_close:
            db.close()


def build_mcp_server(
    config_path: str | None = None,
    *,
    transport: McpTransport = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    allowed_tools: tuple[McpToolName, ...] | None = None,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
    enable_memory_write: bool = False,
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
    if enable_memory_write and transport != "stdio":
        raise ValueError(
            "MCP memory writes are available only over the local stdio transport"
        )
    auth = None
    token_verifier = None
    if auth_token:
        _require_compatible_mcp_sdk(FastMCP)
        issuer_url: Any = f"http://127.0.0.1:{port}"
        auth = AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=issuer_url,
            required_scopes=["sahara:read"],
        )
        token_verifier = StaticTokenVerifier(auth_token)

    server = FastMCP("sahara", host=host, port=port, auth=auth, token_verifier=token_verifier)
    tool_allowlist = set(
        DEFAULT_MCP_TOOLS if allowed_tools is None else allowed_tools
    )
    if enable_memory_write and allowed_tools is None:
        tool_allowlist.add("sahara_remember")
    if "sahara_remember" in tool_allowlist and not enable_memory_write:
        raise ValueError(
            "sahara_remember requires the explicit --enable-memory-write opt-in"
        )

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
                config=config,
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

    if "sahara_recall" in tool_allowlist:
        @server.tool()
        def sahara_recall(
            query: str,
            top_k: int = 5,
            source_types: list[str] | None = None,
            tags: list[str] | None = None,
            since: str | None = None,
            until: str | None = None,
        ) -> list[dict[str, Any]]:
            """Recall managed memories with body snippets and metadata filters."""
            return recall_memories(
                query=query,
                top_k=top_k,
                source_types=tuple(source_types or ()),
                tags=tuple(tags or ()),
                since=since,
                until=until,
                config=config,
                allowed_storage_prefixes=allowed_storage_prefixes,
                max_snippet_chars=max_snippet_chars,
            )

    if "sahara_remember" in tool_allowlist:
        @server.tool()
        def sahara_remember(
            text: str,
            idempotency_key: str,
            explicit_user_request: bool = False,
            title: str | None = None,
            source_type: str = "ai-chat",
            source_url: str = "",
            source_id: str = "",
            tags: list[str] | None = None,
        ) -> dict[str, Any]:
            """Save knowledge only after the user explicitly requests capture."""
            return remember_memory(
                text=text,
                idempotency_key=idempotency_key,
                explicit_user_request=explicit_user_request,
                title=title,
                source_type=source_type,
                source_url=source_url,
                source_id=source_id,
                tags=tuple(tags or ()),
                config=config,
                allowed_storage_prefixes=allowed_storage_prefixes,
            )

    return server


def serve(
    config_path: str | None = None,
    *,
    transport: McpTransport = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    auth_token: str | None = None,
    allowed_tools: tuple[McpToolName, ...] | None = None,
    allowed_storage_prefixes: tuple[str, ...] | None = None,
    max_snippet_chars: int = 500,
    enable_memory_write: bool = False,
) -> None:
    """Run Sahara's MCP server over the requested transport."""
    build_mcp_server(
        config_path=config_path,
        transport=transport,
        host=host,
        port=port,
        auth_token=auth_token,
        allowed_tools=allowed_tools,
        allowed_storage_prefixes=allowed_storage_prefixes,
        max_snippet_chars=max_snippet_chars,
        enable_memory_write=enable_memory_write,
    ).run(transport=transport)
