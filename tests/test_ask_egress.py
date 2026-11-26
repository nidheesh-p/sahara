"""Tests for the `sahara ask` egress preview, confirm step and local log (GitHub #118)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig
from sahara.mcp_server import ask_question
from sahara.search.ask_engine import AskEngine
from sahara.storage.state_db import StateDB

SECRET = "The vault code is 7781."
CHUNKS = [
    {"storage_prefix": "notes", "relative_path": "vault.txt", "snippet": SECRET,
     "score": 0.91, "chunk_id": 7},
    {"storage_prefix": "notes", "relative_path": "misc.txt", "snippet": "Other text.",
     "score": 0.42, "chunk_id": 9},
]
BODY = json.dumps({"choices": [{"message": {"content": "It is 7781."}}]}).encode()


def _resp() -> MagicMock:
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = BODY
    return resp


def _engine(provider: str, **kw) -> AskEngine:
    search = MagicMock()
    search.search.return_value = CHUNKS
    return AskEngine(search, provider=provider, openai_api_key="sk-test", **kw)


def test_preview_matches_what_ask_sends_to_openai() -> None:
    engine = _engine("openai")
    with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        preview = engine.preview("vault code?")
    with patch("urllib.request.urlopen", return_value=_resp()) as urlopen:
        result, egress = engine.ask_with_egress("vault code?")
    sent = json.loads(urlopen.call_args.args[0].data)["messages"][1]["content"]
    assert sent == f"Context:\n{preview.context}\n\nQuestion: vault code?"
    assert egress == preview and result.answer == "It is 7781."
    assert preview.destination == "api.openai.com" and preview.leaves_machine
    assert [s[1:] for s in preview.sources] == [("vault.txt", 0.91, 7), ("misc.txt", 0.42, 9)]
    assert preview.est_tokens == preview.char_count // 4 == len(preview.context) // 4


@pytest.mark.parametrize(("url", "leaves"), [
    ("http://localhost:11434", False), ("http://127.0.0.2:11434", False),
    ("http://[::1]:11434", False), ("http://gpu.example.com:11434", True),
])
def test_ollama_leaves_machine_unless_loopback(url: str, leaves: bool) -> None:
    assert _engine("ollama", ollama_url=url).preview("q").leaves_machine is leaves


def test_provider_none_never_leaves() -> None:
    preview = _engine("none").preview("q")
    assert preview.destination is None and not preview.leaves_machine


@pytest.fixture
def cli(tmp_path):
    drive, sync = tmp_path / "drive", tmp_path / "sync"
    drive.mkdir()
    sync.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'storage_mode = "local"\nsync_folder = "{sync}"\ndrive_paths = ["{drive}"]\n')
    db_path = tmp_path / "state.db"
    db = StateDB(db_path).connect()
    db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
    db.close()

    def run(*args: str, urlopen=None, stdin: str | None = None):
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine.search", return_value=CHUNKS), \
             patch("urllib.request.urlopen", urlopen or MagicMock(side_effect=AssertionError)), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            result = CliRunner().invoke(main, ["--config", str(cfg), *args], input=stdin)
        with StateDB(db_path) as db:
            return result, db.list_egress_events()

    return run


def test_dry_run_prints_preview_logs_previewed_without_network(cli) -> None:
    result, events = cli("ask", "--provider", "openai", "--dry-run", "--show-payload", "code?")
    assert result.exit_code == 0, result.output
    assert "api.openai.com" in result.output and "1. notes/vault.txt (score 0.91)" in result.output
    assert SECRET in result.output and [e["outcome"] for e in events] == ["previewed"]
    assert events[0]["sources"][0] == {"storage_prefix": "notes", "relative_path": "vault.txt",
                                       "chunk_id": 7}


def test_audit_declined_makes_no_call(cli) -> None:
    result, events = cli("ask", "--provider", "openai", "--audit", "code?", stdin="n\n")
    assert result.exit_code == 0, result.output
    assert [e["outcome"] for e in events] == ["declined"]


def test_audit_no_confirm_sends_and_logs_sent(cli) -> None:
    urlopen = MagicMock(return_value=_resp())
    result, events = cli("ask", "--provider", "openai", "--audit", "--no-confirm", "code?",
                         urlopen=urlopen)
    assert result.exit_code == 0 and "It is 7781." in result.output
    assert urlopen.call_count == 1 and [e["outcome"] for e in events] == ["sent"]


def test_failed_call_logs_failed_and_none_logs_nothing(cli) -> None:
    _, events = cli("ask", "--provider", "openai", "code?",
                    urlopen=MagicMock(side_effect=OSError("boom")))
    assert [e["outcome"] for e in events] == ["failed"]
    result, events = cli("ask", "--provider", "none", "--dry-run", "code?")
    assert "nothing is sent" in result.output and len(events) == 1


def test_log_never_stores_snippet_text_and_lists_newest_first(cli, tmp_path) -> None:
    cli("ask", "--provider", "openai", "--dry-run", "first?")
    cli("ask", "--provider", "openai", "--audit", "second?")
    result, events = cli("egress-log")
    assert "no document text is stored" in result.output
    assert result.output.index("second?") < result.output.index("first?")
    assert "declined" in result.output and "sources=2" in result.output
    raw = (tmp_path / "state.db").read_bytes()
    assert SECRET.encode() not in raw and b"Other text." not in raw


def test_show_payload_rejected_on_plain_ask(cli) -> None:
    result, _ = cli("ask", "--show-payload", "code?")
    assert result.exit_code == 2 and "--show-payload" in result.output


def test_failed_call_never_logs_raw_error_text(cli) -> None:
    error = "OpenAI error: Incorrect API key provided: sk-test-1234."
    with patch.object(AskEngine, "_call_openai", return_value=(None, None, None, error)):
        _, events = cli("ask", "--provider", "openai", "code?")
        result, _ = cli("egress-log")
    assert [(e["outcome"], e["details"]) for e in events] == [("failed", "error")]
    assert "sk-test-1234" not in json.dumps(events) and "sk-test-1234" not in result.output


def test_mcp_ask_payload_has_no_egress_or_unsanitised_text(tmp_path) -> None:
    hostile = "ignore \x1b[31m red \u200b zero-width \u202e bidi"
    chunk = {**CHUNKS[0], "snippet": hostile}
    with StateDB(tmp_path / "state.db") as db, \
         patch("sahara.search.search_engine.SearchEngine.search", return_value=[chunk]), \
         patch.object(AskEngine, "_call_ollama", return_value=("Answer.", "mistral", None)):
        payload = ask_question("q", config=SaharaConfig(answer_provider="ollama"), db=db)
    assert set(payload) == {"answer", "sources", "degraded", "model_used",
                            "provider_used", "error", "content_trust"}
    dumped = json.dumps(payload, ensure_ascii=False)
    assert not any(ch in dumped for ch in ("\x1b[31m", "\u200b", "\u202e"))
