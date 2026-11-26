"""Doc-drift guards for the "What leaves your machine" section (GitHub #118)."""

from __future__ import annotations

from pathlib import Path

from sahara.config import SaharaConfig
from sahara.search.ask_engine import (
    _CONTEXT_CHAR_LIMIT,
    _OPENAI_API_URL,
    DEFAULT_MAX_CONTEXT_CHUNKS,
)

ROOT = Path(__file__).resolve().parent.parent
HEADING = "## What leaves your machine"
ANCHOR = "SECURITY.md#what-leaves-your-machine"


def _security() -> str:
    return (ROOT / "SECURITY.md").read_text(encoding="utf-8")


def _section() -> str:
    text = _security()
    start = text.index(HEADING)
    end = text.index("\n## ", start + len(HEADING))
    return text[start:end]


def _table_rows() -> list[str]:
    return [line for line in _section().splitlines() if line.startswith("|")][2:]


def test_section_exists_before_threat_model() -> None:
    text = _security()
    assert HEADING in text
    assert text.index(HEADING) < text.index("## Threat model")


def test_table_names_each_configuration() -> None:
    rows = _table_rows()
    assert len(rows) == 5
    assert 'answer_provider = "none"' in rows[0]
    assert "MCP" in rows[0]
    assert 'answer_provider = "ollama"' in rows[1]
    assert 'answer_provider = "openai"' in rows[2]
    assert "MCP" in rows[3] and "stdio" in rows[3] and "HTTP" in rows[3]
    assert "Claude Desktop" in rows[3]
    for name in ("local drive", "S3", "MinIO"):
        assert name in rows[4]


def test_default_provider_matches_config() -> None:
    default = SaharaConfig().answer_provider
    assert default == "none"
    assert f'`answer_provider = "{default}"` (the default)' in _section()


def test_context_limits_match_code() -> None:
    section = _section()
    assert f"`max_context_chunks` snippets (default {DEFAULT_MAX_CONTEXT_CHUNKS})" in section
    assert f"at most {_CONTEXT_CHAR_LIMIT} characters" in section
    assert _OPENAI_API_URL in section

    providers = (ROOT / "docs" / "ANSWER_PROVIDERS.md").read_text(encoding="utf-8")
    assert f"capped at {DEFAULT_MAX_CONTEXT_CHUNKS}\nsnippets and {_CONTEXT_CHAR_LIMIT} characters" in providers


def test_section_mentions_no_unbuilt_flags() -> None:
    section = _section()
    assert "--dry-run" not in section
    assert "--audit" not in section


def test_readme_and_providers_link_to_section() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    providers = (ROOT / "docs" / "ANSWER_PROVIDERS.md").read_text(encoding="utf-8")
    assert f"({ANCHOR})" in readme
    assert f"(../{ANCHOR})" in providers
