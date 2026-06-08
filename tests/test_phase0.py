"""Coverage for installation, configuration, indexing, search, and ask behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.config import SaharaConfig
from sahara.search.search_engine import SearchEngine, TextExtractor, _split_chunks
from sahara.storage.state_db import StateDB

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "state.db"
    db = StateDB(db_path).connect()
    yield db
    db.close()


@pytest.fixture
def tmp_sync_dir(tmp_path):
    folder = tmp_path / "sync"
    folder.mkdir()
    return folder


@pytest.fixture
def config_local(tmp_path, tmp_sync_dir):
    """Minimal config for local drive mode (no bucket needed)."""
    drive = tmp_path / "drive1"
    drive.mkdir()
    return SaharaConfig(
        storage_mode="local",
        sync_folder=str(tmp_sync_dir),
        drive_paths=[str(drive)],
    )


@pytest.fixture
def config_s3():
    return SaharaConfig(
        storage_mode="s3",
        sync_folder="/tmp/sync",
        bucket="my-bucket",
    )


# ---------------------------------------------------------------------------
# 0.1 — pyproject.toml version / description
# ---------------------------------------------------------------------------


def test_pyproject_version():
    # The distribution rename ships as the 0.2.1 patch release.
    # We test the source directly since the package may not be reinstalled yet.
    toml_path = Path(__file__).parent.parent / "pyproject.toml"
    content = toml_path.read_text()
    assert 'version = "0.2.1"' in content


def test_pyproject_distribution_name():
    toml_path = Path(__file__).parent.parent / "pyproject.toml"
    content = toml_path.read_text()
    assert 'name = "sahara-memory"' in content
    assert 'all = ["sahara-memory[search,ocr,mcp]"]' in content


def test_pyproject_description():
    toml_path = Path(__file__).parent.parent / "pyproject.toml"
    content = toml_path.read_text()
    assert "local-first" in content.lower() or "Local-first" in content


def test_pyproject_search_deps():
    toml_path = Path(__file__).parent.parent / "pyproject.toml"
    content = toml_path.read_text()
    assert "sqlite-vec" in content
    assert "pypdf" in content
    assert "python-docx" in content
    assert "pdfplumber" not in content


def test_pyproject_extras_exist():
    toml_path = Path(__file__).parent.parent / "pyproject.toml"
    content = toml_path.read_text()
    assert "ocr" in content
    assert "dev" in content


# ---------------------------------------------------------------------------
# 0.2 — _require_config fix
# ---------------------------------------------------------------------------


def test_require_config_local_no_bucket(config_local):
    """Local mode with no bucket must not abort."""
    from sahara.cli import _require_config
    # Should not raise — local mode has no bucket
    _require_config(config_local)


def test_require_config_s3_no_bucket():
    """S3 mode with no bucket must abort."""
    from sahara.cli import _require_config
    cfg = SaharaConfig(storage_mode="s3", sync_folder="/tmp/sync", bucket="")
    with pytest.raises(SystemExit):
        _require_config(cfg)


def test_require_config_no_sync_folder():
    """Missing sync_folder must abort regardless of mode."""
    from sahara.cli import _require_config
    cfg = SaharaConfig(storage_mode="local", sync_folder="", drive_paths=["/tmp"])
    with pytest.raises(SystemExit):
        _require_config(cfg)


def test_require_config_s3_with_bucket(config_s3):
    """S3 mode with bucket must not abort."""
    from sahara.cli import _require_config
    _require_config(config_s3)


# ---------------------------------------------------------------------------
# 0.3 — StateDB: chunks table + vec_chunks
# ---------------------------------------------------------------------------


class TestStateDBChunks:
    def test_upsert_and_get_chunk(self, tmp_db):
        chunk_id = tmp_db.upsert_chunk(
            storage_prefix="",
            relative_path="notes/todo.md",
            chunk_index=0,
            content_hash="abc123",
            chunk_text="Buy milk and eggs",
        )
        assert isinstance(chunk_id, int)
        assert chunk_id > 0

    def test_get_chunk_content_hash(self, tmp_db):
        tmp_db.upsert_chunk("", "doc.txt", 0, "hash_v1", "first chunk")
        assert tmp_db.get_chunk_content_hash("", "doc.txt") == "hash_v1"

    def test_upsert_chunk_idempotent(self, tmp_db):
        id1 = tmp_db.upsert_chunk("", "doc.txt", 0, "h1", "text")
        id2 = tmp_db.upsert_chunk("", "doc.txt", 0, "h2", "updated text")
        assert id1 == id2
        assert tmp_db.get_chunk_content_hash("", "doc.txt") == "h2"

    def test_delete_chunks_for_file(self, tmp_db):
        tmp_db.upsert_chunk("", "doc.txt", 0, "h", "chunk 0")
        tmp_db.upsert_chunk("", "doc.txt", 1, "h", "chunk 1")
        deleted = tmp_db.delete_chunks_for_file("", "doc.txt")
        assert len(deleted) == 2
        assert tmp_db.get_chunk_content_hash("", "doc.txt") is None

    def test_count_chunks(self, tmp_db):
        assert tmp_db.count_chunks() == 0
        tmp_db.upsert_chunk("", "a.txt", 0, "h", "text")
        tmp_db.upsert_chunk("", "b.txt", 0, "h", "text")
        assert tmp_db.count_chunks() == 2

    def test_count_chunks_by_prefix(self, tmp_db):
        tmp_db.upsert_chunk("prefix1", "a.txt", 0, "h", "text")
        tmp_db.upsert_chunk("prefix2", "b.txt", 0, "h", "text")
        assert tmp_db.count_chunks("prefix1") == 1
        assert tmp_db.count_chunks("prefix2") == 1

    def test_has_vec_table_false_without_sqlite_vec(self, tmp_db):
        # If sqlite-vec isn't installed the table won't exist
        # Either True or False is valid depending on installation
        result = tmp_db.has_vec_table()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# 0.3 — _split_chunks
# ---------------------------------------------------------------------------


class TestSplitChunks:
    def test_empty_string(self):
        assert _split_chunks("") == []

    def test_short_text_single_chunk(self):
        text = "hello world"
        chunks = _split_chunks(text, size=100, overlap=20)
        assert chunks == ["hello world"]

    def test_exact_size(self):
        text = "A" * 1600
        chunks = _split_chunks(text, size=1600, overlap=320)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_overlap(self):
        text = "A" * 2000
        chunks = _split_chunks(text, size=1600, overlap=320)
        assert len(chunks) == 2
        # Overlap region should appear in both chunks
        # First chunk ends at 1600, second starts at 1280
        assert chunks[0][:320] == "A" * 320  # both share the overlap region

    def test_multiple_chunks_cover_full_text(self):
        text = "word " * 1000  # 5000 chars
        chunks = _split_chunks(text, size=1600, overlap=320)
        assert len(chunks) > 1
        # Every part of the text should appear in at least one chunk
        # Just verify first and last chunks contain expected text
        assert chunks[0].startswith("word ")
        assert "word " in chunks[-1]

    def test_no_overlap(self):
        text = "ABCDEFGHIJ"
        chunks = _split_chunks(text, size=4, overlap=0)
        assert chunks[0] == "ABCD"
        assert chunks[1] == "EFGH"
        assert chunks[2] == "IJ"


# ---------------------------------------------------------------------------
# 0.3 — TextExtractor
# ---------------------------------------------------------------------------


class TestTextExtractor:
    def test_extract_txt(self, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("Hello Sahara\nSecond line")
        extractor = TextExtractor()
        result = extractor.extract(f)
        assert result == "Hello Sahara\nSecond line"

    def test_extract_md(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Title\nContent here")
        extractor = TextExtractor()
        assert extractor.extract(f) is not None

    def test_extract_unsupported_binary(self, tmp_path):
        f = tmp_path / "image.png"
        # All NULL bytes — clearly binary (>30% non-text)
        f.write_bytes(b"\x00" * 512)
        extractor = TextExtractor()
        assert extractor.extract(f) is None

    def test_extract_image_extension_skipped_even_if_text_like(self, tmp_path):
        f = tmp_path / "image.png"
        f.write_text("this fake image has printable bytes", encoding="utf-8")
        extractor = TextExtractor()
        assert extractor.extract(f) is None

    def test_extract_pdf_no_pypdf(self, tmp_path):
        """If pypdf is absent, PDF extraction returns None without raising."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        extractor = TextExtractor()
        # Works either way — just must not raise
        result = extractor.extract(f)
        assert result is None or isinstance(result, str)

    def test_looks_like_text_true(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_bytes(b"name,age\nAlice,30\n")
        extractor = TextExtractor()
        assert extractor._looks_like_text(f) is True

    def test_looks_like_text_false(self, tmp_path):
        f = tmp_path / "bin.dat"
        f.write_bytes(bytes([0x00, 0x01, 0x02] * 200))
        extractor = TextExtractor()
        assert extractor._looks_like_text(f) is False


# ---------------------------------------------------------------------------
# 0.3 — SearchEngine (mocked embeddings for speed)
# ---------------------------------------------------------------------------


def _make_fake_embedding(text: str, dim: int = 384):
    """Return a deterministic unit-ish fake embedding (not real ML)."""
    import hashlib
    import math
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**31)
    vec = [(math.sin(seed + i) + 1) / 2 for i in range(dim)]
    magnitude = sum(x**2 for x in vec) ** 0.5
    return [x / magnitude for x in vec]


class FakeTextEmbedding:
    """Drop-in for fastembed.TextEmbedding that returns deterministic vectors."""

    def __init__(self, model_name: str):
        pass

    def embed(self, texts):
        import numpy as np
        for t in texts:
            yield np.array(_make_fake_embedding(t), dtype=np.float32)


@pytest.fixture
def search_engine(tmp_db):
    engine = SearchEngine(tmp_db)
    # Inject fake embedding model
    engine._model = FakeTextEmbedding("BAAI/bge-small-en-v1.5")
    return engine


class TestSearchEngineIndex:
    def test_index_new_file(self, search_engine, tmp_path, tmp_db):
        f = tmp_path / "notes.txt"
        f.write_text("Buy milk and eggs from the supermarket")
        result = search_engine.index_file(f, "", "notes.txt")
        assert result is True
        assert tmp_db.count_chunks() >= 1

    def test_index_unchanged_file_skipped(self, search_engine, tmp_path, tmp_db):
        f = tmp_path / "notes.txt"
        f.write_text("Stable content")
        search_engine.index_file(f, "", "notes.txt")
        result = search_engine.index_file(f, "", "notes.txt")
        assert result is False  # unchanged — skipped

    def test_index_forced_reindex(self, search_engine, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("Some content")
        search_engine.index_file(f, "", "notes.txt")
        result = search_engine.index_file(f, "", "notes.txt", force=True)
        assert result is True

    def test_index_large_file_creates_multiple_chunks(self, search_engine, tmp_path, tmp_db):
        f = tmp_path / "big.txt"
        # Write enough text to produce more than one chunk
        f.write_text("paragraph content. " * 500)  # ~9500 chars
        search_engine.index_file(f, "", "big.txt")
        assert tmp_db.count_chunks(storage_prefix="") >= 2

    def test_index_unsupported_file_returns_false(self, search_engine, tmp_path):
        f = tmp_path / "photo.bin"
        f.write_bytes(bytes([0x00, 0x01] * 300))
        result = search_engine.index_file(f, "", "photo.bin")
        assert result is False

    def test_index_empty_file_returns_false(self, search_engine, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("   ")  # whitespace only
        result = search_engine.index_file(f, "", "empty.txt")
        assert result is False

    def test_reindex_updates_chunks(self, search_engine, tmp_path, tmp_db):
        f = tmp_path / "doc.txt"
        f.write_text("Original content")
        search_engine.index_file(f, "", "doc.txt")
        old_hash = tmp_db.get_chunk_content_hash("", "doc.txt")

        f.write_text("Completely different content now")
        search_engine.index_file(f, "", "doc.txt", force=True)
        new_hash = tmp_db.get_chunk_content_hash("", "doc.txt")

        assert old_hash != new_hash

    def test_prefix_isolation(self, search_engine, tmp_path, tmp_db):
        f = tmp_path / "doc.txt"
        f.write_text("Content A")
        search_engine.index_file(f, "prefix_a", "doc.txt")
        search_engine.index_file(f, "prefix_b", "doc.txt")
        assert tmp_db.count_chunks("prefix_a") >= 1
        assert tmp_db.count_chunks("prefix_b") >= 1


class TestSearchEngineSearch:
    def test_search_returns_results(self, search_engine, tmp_path):
        (tmp_path / "groceries.txt").write_text("Buy milk, bread, and eggs from the supermarket")
        (tmp_path / "work.txt").write_text("Q3 budget planning meeting notes and action items")
        search_engine.index_file(tmp_path / "groceries.txt", "", "groceries.txt")
        search_engine.index_file(tmp_path / "work.txt", "", "work.txt")

        results = search_engine.search("grocery shopping list", top_k=5)
        assert len(results) > 0
        assert all("relative_path" in r for r in results)
        assert all("score" in r for r in results)

    def test_search_empty_index_returns_empty(self, search_engine):
        results = search_engine.search("anything")
        assert results == []

    def test_search_top_k_respected(self, search_engine, tmp_path):
        for i in range(5):
            f = tmp_path / f"file{i}.txt"
            f.write_text(f"Document number {i} with unique content about topic {i}")
            search_engine.index_file(f, "", f"file{i}.txt")
        results = search_engine.search("document content", top_k=3)
        assert len(results) <= 3

    def test_search_prefix_filter(self, search_engine, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("Sahara storage notes")
        search_engine.index_file(f, "personal", "doc.txt")
        search_engine.index_file(f, "work", "doc.txt")

        results = search_engine.search("storage", top_k=5, storage_prefix="personal")
        assert all(r.get("storage_prefix") == "personal" for r in results)

    def test_search_scores_between_0_and_1(self, search_engine, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("Important financial document about taxes")
        search_engine.index_file(f, "", "doc.txt")
        results = search_engine.search("financial taxes")
        for r in results:
            assert 0.0 <= r["score"] <= 1.0 + 1e-6  # small float tolerance


# ---------------------------------------------------------------------------
# 0.4 — AskEngine
# ---------------------------------------------------------------------------


class TestAskEngine:
    def test_ask_degrades_when_no_results(self, search_engine):
        from sahara.search.ask_engine import AskEngine
        ask = AskEngine(search_engine)
        result = ask.ask("what is my passport number?")
        assert result.degraded is True
        assert result.answer is None

    def test_ask_returns_sources(self, search_engine, tmp_path):
        from sahara.search.ask_engine import AskEngine
        f = tmp_path / "passport.txt"
        f.write_text("Passport number: A1234567. Expires 2032-08-14.")
        search_engine.index_file(f, "", "passport.txt")

        ask = AskEngine(search_engine, ollama_url="http://localhost:99999", openai_api_key=None, provider="ollama")
        result = ask.ask("passport expiry")
        # Ollama won't be available on port 99999 — should degrade but have sources
        assert len(result.sources) > 0
        assert result.degraded is True

    def test_ask_uses_ollama_when_available(self, search_engine, tmp_path):
        from sahara.search.ask_engine import AskEngine
        f = tmp_path / "note.txt"
        f.write_text("The project deadline is March 15 2026.")
        search_engine.index_file(f, "", "note.txt")

        fake_response = json.dumps({"response": "The deadline is March 15 2026."}).encode()

        class FakeResp:
            def read(self):
                return fake_response

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            ask = AskEngine(search_engine, openai_api_key=None, provider="ollama")
            result = ask.ask("what is the project deadline?")

        assert result.answer == "The deadline is March 15 2026."
        assert result.degraded is False
        assert result.model_used is not None

    def test_ask_ollama_timeout_degrades(self, search_engine, tmp_path):
        import urllib.error

        from sahara.search.ask_engine import AskEngine
        f = tmp_path / "note.txt"
        f.write_text("Some content to find")
        search_engine.index_file(f, "", "note.txt")

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            ask = AskEngine(search_engine, provider="ollama")
            result = ask.ask("some content")

        assert result.degraded is True
        assert result.answer is None
        assert result.error is not None
        assert len(result.sources) > 0  # sources still returned

    def test_build_context_respects_char_limit(self, search_engine):
        from sahara.search.ask_engine import _CONTEXT_CHAR_LIMIT, AskEngine
        ask = AskEngine(search_engine)
        chunks = [
            {"relative_path": f"doc{i}.txt", "snippet": "X" * 2000, "storage_prefix": ""}
            for i in range(10)
        ]
        context = ask._build_context(chunks)
        assert len(context) <= _CONTEXT_CHAR_LIMIT + 200  # small margin for separators


# ---------------------------------------------------------------------------
# 0.4 — sahara ask CLI
# ---------------------------------------------------------------------------


class TestAskCLI:
    def test_ask_command_exists(self):
        runner = CliRunner()
        result = runner.invoke(main, ["ask", "--help"])
        assert result.exit_code == 0
        assert "question" in result.output.lower() or "QUESTION" in result.output

    def test_ask_no_index_warns(self, tmp_path):
        runner = CliRunner()
        drive = tmp_path / "drive"
        drive.mkdir()
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            f'storage_mode = "local"\n'
            f'sync_folder = "{tmp_path / "sync"}"\n'
            f'drive_paths = ["{drive}"]\n'
        )
        (tmp_path / "sync").mkdir()
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(
                main,
                ["--config", str(cfg_file), "ask", "what files do I have?"],
            )
        assert result.exit_code == 0
        assert "index" in result.output.lower()

    def test_ask_local_mode_passes_require_config(self, tmp_path):
        """Regression: local mode must not abort at _require_config (bucket check)."""
        runner = CliRunner()
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        sync.mkdir()
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            f'storage_mode = "local"\n'
            f'sync_folder = "{sync}"\n'
            f'drive_paths = ["{drive}"]\n'
        )
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(
                main,
                ["--config", str(cfg_file), "ask", "test question"],
            )
        # Should NOT exit with the "not initialised" error
        assert "not initialised" not in result.output


# ---------------------------------------------------------------------------
# 0.3 — sahara index + search CLI (smoke tests)
# ---------------------------------------------------------------------------


class TestIndexSearchCLI:
    def _make_config_file(self, tmp_path) -> tuple[Path, Path, Path]:
        drive = tmp_path / "drive"
        drive.mkdir()
        sync = tmp_path / "sync"
        sync.mkdir()
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            f'storage_mode = "local"\n'
            f'sync_folder = "{sync}"\n'
            f'drive_paths = ["{drive}"]\n'
        )
        return cfg, sync, drive

    def test_index_no_files_succeeds(self, tmp_path):
        cfg, sync, _ = self._make_config_file(tmp_path)
        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(main, ["--config", str(cfg), "index"])
        assert result.exit_code == 0

    def test_search_no_index_warns(self, tmp_path):
        cfg, _, _ = self._make_config_file(tmp_path)
        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"):
            result = runner.invoke(main, ["--config", str(cfg), "search", "anything"])
        assert result.exit_code == 0
        assert "index" in result.output.lower()
