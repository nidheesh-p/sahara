"""Doc-drift guards for the "What leaves your machine" section (GitHub #118).

Each test ties a number or name in the docs to the code, so the section cannot
silently go stale. Prose is compared with whitespace collapsed, so rewrapping a
paragraph never breaks a test.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from sahara.cli import mcp_serve
from sahara.config import SaharaConfig
from sahara.mcp_server import ask_question
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
    return " ".join(text[start : text.index("\n## ", start + len(HEADING))].split())


def test_section_precedes_threat_model() -> None:
    text = _security()
    assert text.index(HEADING) < text.index("## Threat model")


def test_documented_defaults_and_limits_match_code() -> None:
    section = _section()
    assert SaharaConfig().answer_provider == "none"
    assert '`answer_provider = "none"` (the default)' in section
    assert f"`max_context_chunks` snippets (default {DEFAULT_MAX_CONTEXT_CHUNKS})" in section
    assert f"at most {_CONTEXT_CHAR_LIMIT} characters" in section
    assert _OPENAI_API_URL in section


def test_mcp_cannot_choose_the_provider_and_documented_flag_exists() -> None:
    assert "provider" not in inspect.signature(ask_question).parameters
    assert "--allow-tool" in {opt for p in mcp_serve.params for opt in p.opts}
    assert "--allow-tool" in _section()


def test_readme_and_providers_link_to_section() -> None:
    for path, prefix in (("README.md", ""), ("docs/ANSWER_PROVIDERS.md", "../")):
        assert f"({prefix}{ANCHOR})" in (ROOT / path).read_text(encoding="utf-8")
