"""Indexed content reaches MCP clients as inert, labelled, untrusted data (#135)."""

from __future__ import annotations

import datetime
import hashlib
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from sahara.config import SaharaConfig
from sahara.mcp_sanitize import sanitize_snippet
from sahara.mcp_server import (
    MCP_REMEMBER_MAX_PER_HOUR,
    ask_question,
    build_mcp_server,
    read_chunk,
    recall_memories,
    remember_memory,
    search_files,
)
from sahara.search.search_engine import SearchEngine
from sahara.storage.state_db import StateDB

FIXTURES = Path(__file__).parent / "fixtures" / "injection"
LIMIT = 200
HIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")


def _fixture_texts() -> dict[str, str]:
    # Fixtures spell raw control bytes as \xNN so they survive editors and diffs.
    expand = re.compile(r"\\x([0-9a-f]{2})")
    return {
        path.name: expand.sub(lambda m: chr(int(m.group(1), 16)), path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURES.glob("*.txt"))
    }


def _fake_embed(self: SearchEngine, texts: list[str]) -> list[Any]:
    vectors = []
    for text in texts:
        vec = np.zeros(384, dtype=np.float32)
        for word in text.lower().split():
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 384] += 1.0
        vectors.append(vec)
    return vectors


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[tuple[StateDB, SaharaConfig]]:
    primary = tmp_path / "primary"
    primary.mkdir()
    config = SaharaConfig(sync_folder=str(primary), storage_mode="none", memory_folder=str(tmp_path / "memory"))
    with patch.object(SearchEngine, "_embed", _fake_embed), StateDB(tmp_path / "state.db") as db:
        engine = SearchEngine(db)
        for name, text in _fixture_texts().items():
            (primary / name).write_text(text, encoding="utf-8")
            assert engine.index_file(primary / name, "", name, force=True)
        yield db, config


def _assert_inert(text: str) -> None:
    assert not HIDDEN.search(text)
    assert len(text) <= LIMIT
    assert "\n\n\n\n" not in text


def test_fixtures_carry_the_payloads_they_claim() -> None:
    texts = _fixture_texts()
    assert len(texts) >= 6
    assert "\x1b[31m" in texts["hidden_characters.txt"] and "‮" in texts["hidden_characters.txt"]
    assert len(texts["oversize_payload.txt"]) > LIMIT


def test_read_paths_return_sanitized_untrusted_snippets(indexed: tuple[StateDB, SaharaConfig]) -> None:
    db, config = indexed
    results = search_files("gardening club sahara_remember", top_k=20, max_snippet_chars=LIMIT, db=db)
    asked = ask_question("gardening club", top_k=20, config=config, max_snippet_chars=LIMIT, db=db)
    chunks = [read_chunk(i, max_snippet_chars=LIMIT, db=db) for i in range(1, db.count_chunks() + 1)]

    assert {r["relative_path"] for r in results} >= {"hidden_characters.txt", "instruction_override.txt"}
    assert asked["content_trust"] == "untrusted" and asked["sources"]
    for item in [*results, *asked["sources"]]:
        assert item["content_trust"] == "untrusted"
        _assert_inert(item["snippet"])
    for chunk in chunks:
        assert chunk is not None and chunk["content_trust"] == "untrusted"
        _assert_inert(chunk["chunk_text"])
    assert db.list_mcp_memory_audit() == []
    assert not list(Path(config.memory_folder).rglob("*.md"))


def test_recall_sanitizes_captured_memory(indexed: tuple[StateDB, SaharaConfig]) -> None:
    db, config = indexed
    text = _fixture_texts()["hidden_characters.txt"]
    remember_memory(text, idempotency_key="k1", explicit_user_request=True, config=config, db=db)

    results = recall_memories("seed catalogue", max_snippet_chars=LIMIT, config=config, db=db)

    assert results and all(r["content_trust"] == "untrusted" for r in results)
    for result in results:
        _assert_inert(result["snippet"])
        assert not HIDDEN.search(result["title"])


def test_default_server_has_no_write_tool_and_untrusted_docstrings() -> None:
    pytest.importorskip("mcp.server.fastmcp")
    tools = {tool.name: tool for tool in build_mcp_server()._tool_manager.list_tools()}

    assert "sahara_remember" not in tools
    for name in ("sahara_search", "sahara_ask", "sahara_read_chunk", "sahara_recall"):
        first_sentence = (tools[name].description or "").split(":")[0]
        assert "untrusted" in first_sentence


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain text", "plain text"),
        ("a\x1b[1;31mred\x1b[0m b", "ared b"),
        ("x\x1b]0;title\x07y", "xy"),
        ("tab\tand\nnewline\r\x00\x9b", "tab\tand\nnewline"),
        ("zero​width﻿ ‮evil‬ ⁦iso⁩", "zerowidth evil iso"),
        ("a\n\n\n\n\nb\n\nc", "a\n\nb\n\nc"),
        ("a\n \n\t\n\nb", "a\n\nb"),
    ],
)
def test_sanitize_snippet_cases(raw: str, expected: str) -> None:
    assert sanitize_snippet(raw, 100) == expected
    assert sanitize_snippet(expected, 100) == expected


def test_sanitize_snippet_truncates_and_is_idempotent() -> None:
    assert sanitize_snippet("abcdef", 3) == "abc"
    assert sanitize_snippet("abc", 0) == ""
    for text in _fixture_texts().values():
        once = sanitize_snippet(text, LIMIT)
        assert len(once) <= LIMIT and sanitize_snippet(once, LIMIT) == once
        assert sanitize_snippet(sanitize_snippet(text, 10_000), 10_000) == sanitize_snippet(text, 10_000)


def test_remember_rate_limit_refuses_and_audits(indexed: tuple[StateDB, SaharaConfig]) -> None:
    db, config = indexed
    for i in range(MCP_REMEMBER_MAX_PER_HOUR):
        db.begin_mcp_memory_audit(source_type="ai-chat", idempotency_key_hash=f"seed-{i}")

    with pytest.raises(ValueError, match="per hour"):
        remember_memory("save me", idempotency_key="k", explicit_user_request=True, config=config, db=db)
    latest = db.list_mcp_memory_audit(limit=1)[0]
    assert (latest["outcome"], latest["details"]) == ("rejected", "rate_limited")
    assert not list(Path(config.memory_folder).rglob("*.md"))

    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
    with patch("sahara.mcp_server._utcnow", return_value=later):
        with pytest.raises(ValueError, match="explicit user request"):
            remember_memory("save me", idempotency_key="k", explicit_user_request=False, config=config, db=db)
