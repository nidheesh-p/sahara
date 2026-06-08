"""Tests for AskEngine OpenAI provider support."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sahara.search.ask_engine import (
    DEFAULT_OPENAI_MODEL,
    AskEngine,
)


def _make_engine(provider=None, openai_api_key=None, model=None, **kw):
    search = MagicMock()
    search.search.return_value = [
        {"relative_path": "doc.txt", "snippet": "The answer is 42.", "score": 0.9}
    ]
    return AskEngine(
        search,
        provider=provider,
        openai_api_key=openai_api_key,
        model=model,
        **kw,
    )


class TestProviderSelection:
    def test_defaults_to_none_without_key(self):
        with patch.dict("os.environ", {}, clear=True):
            engine = _make_engine()
        assert engine._provider == "none"
        assert engine._model is None

    def test_openai_key_does_not_change_none_default(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            engine = _make_engine()
        assert engine._provider == "none"
        assert engine._model is None

    def test_explicit_provider_overrides_env(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            engine = _make_engine(provider="ollama")
        assert engine._provider == "ollama"

    def test_explicit_model_overrides_default(self):
        engine = _make_engine(provider="openai", openai_api_key="sk-test", model="gpt-4o")
        assert engine._model == "gpt-4o"

    def test_openai_key_passed_directly_requires_explicit_provider(self):
        with patch.dict("os.environ", {}, clear=True):
            engine = _make_engine(openai_api_key="sk-direct")
        assert engine._provider == "none"
        assert engine._openai_api_key == "sk-direct"

    def test_none_provider_returns_sources_without_network_call(self):
        engine = _make_engine()
        with patch("urllib.request.urlopen") as urlopen:
            result = engine.ask("what is the answer?")

        assert result.answer is None
        assert not result.degraded
        assert result.sources[0]["relative_path"] == "doc.txt"
        assert result.error is None
        assert result.provider_used is None
        assert result.model_used is None
        urlopen.assert_not_called()


class TestCallOpenAI:
    def _fake_response(self, content: str) -> MagicMock:
        body = {
            "choices": [{"message": {"content": content}}],
            "model": DEFAULT_OPENAI_MODEL,
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(body).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_openai_returns_answer(self):
        engine = _make_engine(provider="openai", openai_api_key="sk-test")
        with patch("urllib.request.urlopen", return_value=self._fake_response("42")):
            result = engine.ask("what is the answer?")
        assert result.answer == "42"
        assert result.provider_used == "openai"
        assert result.model_used == DEFAULT_OPENAI_MODEL
        assert not result.degraded

    def test_openai_no_api_key_degrades(self):
        with patch.dict("os.environ", {}, clear=True):
            engine = _make_engine(provider="openai", openai_api_key=None)
        result = engine.ask("anything?")
        assert result.degraded
        assert result.answer is None
        assert "OPENAI_API_KEY" in result.error

    def test_openai_http_error_degrades(self):
        import urllib.error
        engine = _make_engine(provider="openai", openai_api_key="sk-test")
        err_body = json.dumps({"error": {"message": "invalid_api_key"}}).encode()
        http_err = urllib.error.HTTPError(
            url="https://api.openai.com/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=err_body)),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            result = engine.ask("anything?")
        assert result.degraded
        assert "invalid_api_key" in result.error

    def test_openai_url_error_degrades(self):
        import urllib.error
        engine = _make_engine(provider="openai", openai_api_key="sk-test")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            result = engine.ask("anything?")
        assert result.degraded
        assert result.error is not None

    def test_openai_generic_exception_degrades(self):
        engine = _make_engine(provider="openai", openai_api_key="sk-test")
        with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
            result = engine.ask("anything?")
        assert result.degraded
        assert "boom" in result.error

    def test_openai_sends_correct_headers(self):
        engine = _make_engine(provider="openai", openai_api_key="sk-mykey")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            return self._fake_response("answer")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            engine.ask("question?")

        assert captured["headers"].get("Authorization") == "Bearer sk-mykey"
        assert "application/json" in captured["headers"].get("Content-type", "")

    def test_openai_request_payload_structure(self):
        engine = _make_engine(provider="openai", openai_api_key="sk-test")
        payloads = []

        def fake_urlopen(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return self._fake_response("42")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            engine.ask("what is 6 * 7?")

        assert payloads
        payload = payloads[0]
        assert payload["model"] == DEFAULT_OPENAI_MODEL
        assert any(m["role"] == "system" for m in payload["messages"])
        assert any(m["role"] == "user" for m in payload["messages"])
        assert payload["temperature"] == 0.7
        assert payload["max_tokens"] == 800


class TestCLILocalPrefix:
    """'local' as first word of question forces Ollama provider."""

    def _write_config(self, tmp_path):
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        sync.mkdir()
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'storage_mode = "local"\nsync_folder = "{sync}"\ndrive_paths = ["{drive}"]\n'
        )
        return cfg, sync

    def test_local_prefix_routes_to_ollama(self, tmp_path):
        import numpy as np
        from click.testing import CliRunner

        from sahara.cli import main
        from sahara.storage.state_db import StateDB

        cfg, _ = self._write_config(tmp_path)
        db_path = tmp_path / "state.db"
        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
        db.close()

        ollama_body = json.dumps({"response": "Local answer"}).encode()

        class FakeResp:
            def read(self):
                return ollama_body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        captured_urls = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return FakeResp()

        runner = CliRunner()
        # OPENAI_API_KEY is set, but "local" prefix should force Ollama
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(
                main,
                ["--config", str(cfg), "ask", "local", "what is the answer?"],
            )
        assert result.exit_code == 0
        # Must have called Ollama, not OpenAI
        assert any("ollama" in url or "11434" in url for url in captured_urls)
        assert not any("openai.com" in url for url in captured_urls)

    def test_local_prefix_case_insensitive(self, tmp_path):
        """LOCAL (uppercase) also triggers Ollama routing."""
        import numpy as np
        from click.testing import CliRunner

        from sahara.cli import main
        from sahara.storage.state_db import StateDB

        cfg, _ = self._write_config(tmp_path)
        db_path = tmp_path / "state.db"
        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
        db.close()

        ollama_body = json.dumps({"response": "Local answer"}).encode()

        class FakeResp:
            def read(self):
                return ollama_body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen") as mock_url:
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            mock_url.return_value = FakeResp()
            result = runner.invoke(
                main,
                ["--config", str(cfg), "ask", "LOCAL", "what is the answer?"],
            )
        assert result.exit_code == 0
        # urlopen should have been called (Ollama path, not short-circuit)
        assert mock_url.called


class TestCLIProviderFlag:
    """Integration: --provider flag routes to OpenAI in ask command."""

    def _write_config(self, tmp_path):
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        sync.mkdir()
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'storage_mode = "local"\nsync_folder = "{sync}"\ndrive_paths = ["{drive}"]\n'
        )
        return cfg, sync

    def test_cli_ask_openai_provider_flag(self, tmp_path):
        import numpy as np
        from click.testing import CliRunner

        from sahara.cli import main
        from sahara.storage.state_db import StateDB

        cfg, _ = self._write_config(tmp_path)
        db_path = tmp_path / "state.db"
        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
        db.close()

        body = json.dumps({"choices": [{"message": {"content": "The answer is 42."}}]}).encode()

        class FakeResp:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen", return_value=FakeResp()), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(
                main,
                ["--config", str(cfg), "ask", "--provider", "openai", "what is the answer?"],
            )
        assert result.exit_code == 0
        assert "OpenAI" in result.output or "42" in result.output

    def test_cli_ask_uses_configured_openai_provider(self, tmp_path):
        import numpy as np
        from click.testing import CliRunner

        from sahara.cli import main
        from sahara.storage.state_db import StateDB

        cfg, _ = self._write_config(tmp_path)
        cfg.write_text(
            cfg.read_text() + 'answer_provider = "openai"\nanswer_model = "gpt-configured"\n'
        )
        db_path = tmp_path / "state.db"
        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
        db.close()

        body = json.dumps({"choices": [{"message": {"content": "Configured OpenAI"}}]}).encode()

        class FakeResp:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        captured_payloads = []

        def fake_urlopen(req, timeout=None):
            captured_payloads.append(json.loads(req.data.decode()))
            return FakeResp()

        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = CliRunner().invoke(
                main,
                ["--config", str(cfg), "ask", "what is the answer?"],
            )

        assert result.exit_code == 0
        assert "Configured OpenAI" in result.output
        assert captured_payloads[0]["model"] == "gpt-configured"

    def test_cli_ask_defaults_to_retrieval_only_with_openai_key_in_env(self, tmp_path):
        import numpy as np
        from click.testing import CliRunner

        from sahara.cli import main
        from sahara.storage.state_db import StateDB

        cfg, _ = self._write_config(tmp_path)
        db_path = tmp_path / "state.db"
        db = StateDB(db_path).connect()
        db.upsert_embedding("", "doc.txt", "h", json.dumps([0.5] * 384), "snippet")
        db.close()

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen") as mock_urlopen, \
             patch.dict(
                 "os.environ",
                 {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-test"},
             ):
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(
                main,
                ["--config", str(cfg), "ask", "what is the answer?"],
            )
        assert result.exit_code == 0
        assert "Standalone answer generation is off" in result.output
        assert "doc.txt" in result.output
        mock_urlopen.assert_not_called()
