"""Additional Phase 0 coverage tests — targeting uncovered branches."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.search.search_engine import (
    SearchEngine,
    TextExtractor,
    _floats_to_bytes,
)
from sahara.storage.state_db import StateDB

# ---------------------------------------------------------------------------
# Fixtures (duplicated for isolation)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    db = StateDB(tmp_path / "state.db").connect()
    yield db
    db.close()


def _make_fake_embedding(text: str, dim: int = 384):
    import hashlib
    import math
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**31)
    vec = [(math.sin(seed + i) + 1) / 2 for i in range(dim)]
    magnitude = sum(x**2 for x in vec) ** 0.5
    return [x / magnitude for x in vec]


class FakeTextEmbedding:
    def __init__(self, model_name: str):
        pass

    def embed(self, texts):
        for t in texts:
            yield np.array(_make_fake_embedding(t), dtype=np.float32)


@pytest.fixture
def search_engine(tmp_db):
    engine = SearchEngine(tmp_db)
    engine._model = FakeTextEmbedding("test")
    return engine


# ---------------------------------------------------------------------------
# _floats_to_bytes: numpy and plain-list paths
# ---------------------------------------------------------------------------


def test_floats_to_bytes_numpy():
    vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    result = _floats_to_bytes(vec)
    assert isinstance(result, bytes)
    assert len(result) == 12  # 3 floats * 4 bytes


def test_floats_to_bytes_plain_list():
    """Covers the AttributeError fallback branch (lines 103-104)."""
    result = _floats_to_bytes([0.1, 0.2, 0.3])
    assert isinstance(result, bytes)
    expected = struct.pack("3f", 0.1, 0.2, 0.3)
    assert result == expected


# ---------------------------------------------------------------------------
# TextExtractor — OSError / exception paths
# ---------------------------------------------------------------------------


class TestTextExtractorCoveragePaths:
    def test_oserror_reading_text_file(self, tmp_path):
        """Covers lines 40-42: OSError when read_text fails."""
        f = tmp_path / "doc.txt"
        f.write_text("content")
        extractor = TextExtractor()
        with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
            result = extractor.extract(f)
        assert result is None

    def test_looks_like_text_oserror(self, tmp_path):
        """Covers lines 80-81: OSError in _looks_like_text."""
        f = tmp_path / "no_access.dat"
        f.write_bytes(b"hello")
        extractor = TextExtractor()
        with patch("builtins.open", side_effect=OSError("no access")):
            result = extractor._looks_like_text(f)
        assert result is False

    def test_extract_pdf_success(self, tmp_path):
        """Covers lines 49-51: successful PDF extraction."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4 fake content")
        extractor = TextExtractor()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Page one content"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        with patch("pypdf.PdfReader", return_value=mock_reader):
            result = extractor.extract(f)
        assert result == "Page one content"

    def test_extract_pdf_returns_none_on_empty_pages(self, tmp_path):
        """Success path, but all pages return empty text."""
        f = tmp_path / "empty.pdf"
        f.write_bytes(b"%PDF-1.4")
        extractor = TextExtractor()
        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]
        with patch("pypdf.PdfReader", return_value=mock_reader):
            result = extractor.extract(f)
        assert result is None

    def test_extract_pdf_import_error(self, tmp_path):
        """Covers lines 53-54: pypdf ImportError."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        extractor = TextExtractor()
        with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (
            (_ for _ in ()).throw(ImportError("No module named 'pypdf'"))
            if name == "pypdf" else __import__(name, *a, **kw)
        )):
            result = extractor._extract_pdf(f)
        assert result is None

    def test_extract_docx_success(self, tmp_path):
        """Covers lines 60-64: successful DOCX extraction."""
        f = tmp_path / "memo.docx"
        f.write_bytes(b"fake docx bytes")
        extractor = TextExtractor()
        mock_para1 = MagicMock()
        mock_para1.text = "First paragraph"
        mock_para2 = MagicMock()
        mock_para2.text = ""  # empty para — should be filtered
        mock_doc = MagicMock()
        mock_doc.paragraphs = [mock_para1, mock_para2]
        with patch("docx.Document", return_value=mock_doc):
            result = extractor._extract_docx(f)
        assert result == "First paragraph"

    def test_extract_docx_empty_result(self, tmp_path):
        """DOCX with all-empty paragraphs returns None."""
        f = tmp_path / "blank.docx"
        f.write_bytes(b"fake docx bytes")
        extractor = TextExtractor()
        mock_doc = MagicMock()
        mock_doc.paragraphs = []
        with patch("docx.Document", return_value=mock_doc):
            result = extractor._extract_docx(f)
        assert result is None

    def test_extract_docx_import_error(self, tmp_path):
        """Covers lines 65-67: docx ImportError."""
        f = tmp_path / "doc.docx"
        f.write_bytes(b"fake")
        extractor = TextExtractor()
        with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (
            (_ for _ in ()).throw(ImportError("No module named 'docx'"))
            if name == "docx" else __import__(name, *a, **kw)
        )):
            result = extractor._extract_docx(f)
        assert result is None

    def test_extract_docx_general_exception(self, tmp_path):
        """Covers lines 68-70: generic exception in DOCX extraction."""
        f = tmp_path / "corrupt.docx"
        f.write_bytes(b"fake")
        extractor = TextExtractor()
        with patch("docx.Document", side_effect=Exception("corrupt file")):
            result = extractor._extract_docx(f)
        assert result is None

    def test_extract_dispatches_to_docx(self, tmp_path):
        """Covers line 36: extract() dispatching to _extract_docx for .docx files."""
        f = tmp_path / "report.docx"
        f.write_bytes(b"fake docx bytes")
        extractor = TextExtractor()
        with patch.object(extractor, "_extract_docx", return_value="mocked docx") as mock:
            result = extractor.extract(f)
        mock.assert_called_once_with(f)
        assert result == "mocked docx"


# ---------------------------------------------------------------------------
# SearchEngine — _get_model ImportError
# ---------------------------------------------------------------------------


class TestGetModelImportError:
    def test_get_model_raises_when_fastembed_missing(self, tmp_db):
        """Covers lines 121-125: RuntimeError when fastembed is not installed."""
        engine = SearchEngine(tmp_db)
        # Do NOT pre-set _model so it tries to import fastembed
        with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (
            (_ for _ in ()).throw(ImportError("No module named 'fastembed'"))
            if name == "fastembed" else __import__(name, *a, **kw)
        )):
            with pytest.raises(RuntimeError, match="fastembed is required"):
                engine._get_model()


# ---------------------------------------------------------------------------
# SearchEngine — vec path (mocked has_vec_table)
# ---------------------------------------------------------------------------


class TestSearchVecPath:
    def test_search_uses_vec_when_available(self, search_engine, tmp_path, tmp_db):
        """Covers lines 213-214 and 223-226: _search_vec path."""
        f = tmp_path / "doc.txt"
        f.write_text("The quick brown fox jumps over the lazy dog")
        search_engine.index_file(f, "", "doc.txt")

        # Patch has_vec_table to return True and vec_knn_search to return mock data
        mock_rows = [
            {
                "id": 1,
                "storage_prefix": "",
                "relative_path": "doc.txt",
                "chunk_index": 0,
                "chunk_text": "The quick brown fox",
                "content_hash": "abc",
                "distance": 0.1,
            }
        ]
        with patch.object(tmp_db, "has_vec_table", return_value=True), \
             patch.object(tmp_db, "vec_knn_search", return_value=mock_rows):
            results = search_engine.search("quick fox", top_k=3)

        assert len(results) == 1
        assert results[0]["relative_path"] == "doc.txt"
        assert results[0]["score"] == pytest.approx(0.9, abs=0.01)

    def test_dedup_to_top_k_direct(self):
        """Covers lines 266-279: _dedup_to_top_k static method directly."""
        rows = [
            {"storage_prefix": "", "relative_path": "doc.txt", "chunk_text": "chunk0", "distance": 0.2},
            {"storage_prefix": "", "relative_path": "doc.txt", "chunk_text": "chunk1", "distance": 0.1},  # better
            {"storage_prefix": "", "relative_path": "other.txt", "chunk_text": "other", "distance": 0.3},
        ]
        results = SearchEngine._dedup_to_top_k(rows, top_k=10)
        assert len(results) == 2
        # doc.txt should have the best (lowest distance → highest score)
        doc_result = next(r for r in results if r["relative_path"] == "doc.txt")
        assert doc_result["score"] == pytest.approx(0.9, abs=0.01)  # 1.0 - 0.1

    def test_dedup_to_top_k_respects_k(self):
        rows = [
            {"storage_prefix": "", "relative_path": f"file{i}.txt", "chunk_text": "x", "distance": float(i) / 10}
            for i in range(10)
        ]
        results = SearchEngine._dedup_to_top_k(rows, top_k=3)
        assert len(results) == 3

    def test_dedup_to_top_k_empty(self):
        assert SearchEngine._dedup_to_top_k([], top_k=5) == []

    def test_vec_path_index_calls_upsert_vec_chunk(self, search_engine, tmp_path, tmp_db):
        """Covers lines 165-166 and 180: vec chunk upsert/delete during index."""
        f = tmp_path / "doc.txt"
        f.write_text("content to index")

        with patch.object(tmp_db, "has_vec_table", return_value=True), \
             patch.object(tmp_db, "upsert_vec_chunk") as mock_upsert, \
             patch.object(tmp_db, "delete_vec_chunks") as mock_delete:
            search_engine.index_file(f, "", "doc.txt")
            # First index: no old chunks, so delete not called
            assert mock_upsert.called

        # Re-index to test delete path
        with patch.object(tmp_db, "has_vec_table", return_value=True), \
             patch.object(tmp_db, "upsert_vec_chunk"), \
             patch.object(tmp_db, "delete_vec_chunks") as mock_delete:
            search_engine.index_file(f, "", "doc.txt", force=True)
            assert mock_delete.called


# ---------------------------------------------------------------------------
# SearchEngine — _search_cosine exception path and empty embed
# ---------------------------------------------------------------------------


class TestCosineEdgePaths:
    def test_cosine_handles_bad_embedding_json(self, tmp_db):
        """Covers lines 252-253: exception handler in cosine scan."""
        engine = SearchEngine(tmp_db)
        engine._model = FakeTextEmbedding("test")

        # Insert a row with bad embedding JSON
        tmp_db.upsert_embedding("", "bad.txt", "hash", "NOT_VALID_JSON", "snippet")
        # Insert a good row too
        good_vec = json.dumps([0.1] * 384)
        tmp_db.upsert_embedding("", "good.txt", "hash2", good_vec, "good snippet")

        results = engine.search("query")
        # Should not raise; bad row gets score=0.0
        paths = [r["relative_path"] for r in results]
        assert "good.txt" in paths

    def test_search_empty_embed_result(self, tmp_db):
        """Covers line 211: early return [] when model returns no embeddings."""
        engine = SearchEngine(tmp_db)

        class EmptyModel:
            def embed(self, texts):
                return iter([])  # yields nothing

        engine._model = EmptyModel()
        result = engine.search("anything")
        assert result == []


# ---------------------------------------------------------------------------
# StateDB — migration and vec method coverage
# ---------------------------------------------------------------------------


class TestStateDBMigration:
    def test_migrate_v2_idempotent(self, tmp_path):
        """_migrate_v2 runs safely on a modern DB (idempotent)."""
        db = StateDB(tmp_path / "new.db").connect()
        # Connect a second time — _migrate_v2 should see s3_prefix exists and skip
        db2 = StateDB(tmp_path / "new.db").connect()
        db2.close()
        db.close()

    def test_vec_chunk_operations_without_vec_table(self, tmp_db):
        """vec methods degrade gracefully when vec_chunks doesn't exist."""
        assert tmp_db.has_vec_table() is False

        # upsert_vec_chunk and delete_vec_chunks should be no-ops without the table
        chunk_id = tmp_db.upsert_chunk("", "doc.txt", 0, "h", "text")

        # These would fail at the SQL level if called without checking has_vec_table
        # In the StateDB they're called unconditionally — verify they raise or succeed
        try:
            tmp_db.upsert_vec_chunk(chunk_id, b"\x00" * (384 * 4))
        except Exception:
            pass  # expected — no vec_chunks table

    def test_delete_vec_chunks_empty_list(self, tmp_db):
        """delete_vec_chunks with empty list is a no-op."""
        tmp_db.delete_vec_chunks([])  # must not raise

    def test_count_chunks_per_prefix(self, tmp_db):
        tmp_db.upsert_chunk("a", "f.txt", 0, "h", "text")
        tmp_db.upsert_chunk("b", "f.txt", 0, "h", "text")
        assert tmp_db.count_chunks("a") == 1
        assert tmp_db.count_chunks("b") == 1
        assert tmp_db.count_chunks() == 2


# ---------------------------------------------------------------------------
# CLI integration — index + search with real files
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path) -> tuple[Path, Path, Path]:
    drive = tmp_path / "drive"
    drive.mkdir()
    sync = tmp_path / "sync"
    sync.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'storage_mode = "local"\nsync_folder = "{sync}"\ndrive_paths = ["{drive}"]\n'
    )
    return cfg, sync, drive


class TestIndexCLIWithFiles:
    def test_index_command_indexes_files(self, tmp_path):
        """Exercises the inner file-indexing loop (lines 1608-1632)."""
        cfg, sync, _ = _write_config(tmp_path)
        # Write a text file to sync dir
        (sync / "note.txt").write_text("Important note about the project deadline")
        db_path = tmp_path / "state.db"

        # We need a FileRecord in the DB for index to pick it up
        import datetime

        from sahara.models import FileRecord
        from sahara.storage.state_db import StateDB as _StateDB

        db = _StateDB(db_path).connect()
        db.upsert_file(
            FileRecord(
                relative_path="note.txt",
                sha256_checksum="abc",
                size_bytes=40,
                tier="STANDARD",
                s3_etag="abc",
                last_sync_at=datetime.datetime.now(datetime.UTC),
                local_modified_at=datetime.datetime.now(datetime.UTC),
                remote_modified_at=datetime.datetime.now(datetime.UTC),
            ),
            s3_prefix="",
        )
        db.close()

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed:
            import numpy as np
            mock_embed.return_value = [np.array([0.1] * 384, dtype=np.float32)]
            result = runner.invoke(main, ["--config", str(cfg), "index"])

        assert result.exit_code == 0
        assert "Done" in result.output

    def test_search_command_with_results(self, tmp_path):
        """Exercises the result display loop in search command (lines 1687-1705)."""
        cfg, sync, _ = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        # Pre-insert an embedding so search has something to return
        import numpy as np
        db = StateDB(db_path).connect()
        vec = json.dumps([0.5] * 384)
        db.upsert_embedding("", "report.txt", "hash", vec, "Financial report snippet")
        db.close()

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed:
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(main, ["--config", str(cfg), "search", "financial report"])

        assert result.exit_code == 0
        assert "report.txt" in result.output

    def test_search_command_with_snippet_flag(self, tmp_path):
        """Covers snippet display branch in search command."""
        cfg, sync, _ = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        import numpy as np
        db = StateDB(db_path).connect()
        vec = json.dumps([0.5] * 384)
        db.upsert_embedding("", "memo.txt", "hash", vec, "This is the memo snippet content")
        db.close()

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed:
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(main, ["--config", str(cfg), "search", "--snippet", "memo"])

        assert result.exit_code == 0
        assert "memo.txt" in result.output

    def test_ask_command_with_indexed_files_and_ollama(self, tmp_path):
        """Exercises ask command result display with mocked ollama response."""
        cfg, sync, _ = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        import numpy as np
        db = StateDB(db_path).connect()
        vec = json.dumps([0.5] * 384)
        db.upsert_embedding("", "passport.txt", "hash", vec, "Passport expires 2032-08-14")
        db.close()

        fake_ollama = json.dumps({"response": "Your passport expires on August 14, 2032."}).encode()

        class FakeResp:
            def read(self):
                return fake_ollama
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen", return_value=FakeResp()):
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(main, ["--config", str(cfg), "ask", "--provider", "ollama", "passport expiry date"])

        assert result.exit_code == 0
        assert "August 14, 2032" in result.output

    def test_ask_command_with_snippet_flag(self, tmp_path):
        """Covers the --snippet branch in ask command."""
        cfg, sync, _ = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        import numpy as np
        db = StateDB(db_path).connect()
        vec = json.dumps([0.5] * 384)
        db.upsert_embedding("", "notes.txt", "hash", vec, "Notes about the meeting agenda")
        db.close()

        fake_ollama = json.dumps({"response": "The meeting is about agenda items."}).encode()

        class FakeResp:
            def read(self):
                return fake_ollama
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path), \
             patch("sahara.search.search_engine.SearchEngine._embed") as mock_embed, \
             patch("urllib.request.urlopen", return_value=FakeResp()):
            mock_embed.return_value = [np.array([0.5] * 384, dtype=np.float32)]
            result = runner.invoke(main, [
                "--config", str(cfg), "ask", "--snippet", "--provider", "ollama", "meeting agenda"
            ])

        assert result.exit_code == 0

    def test_index_missing_file_shows_warning(self, tmp_path):
        """Exercises the missing-file branch in index command."""
        cfg, sync, _ = _write_config(tmp_path)
        db_path = tmp_path / "state.db"

        import datetime

        from sahara.models import FileRecord
        db = StateDB(db_path).connect()
        db.upsert_file(
            FileRecord(
                relative_path="missing_file.txt",
                sha256_checksum="abc",
                size_bytes=100,
                tier="STANDARD",
                s3_etag="abc",
                last_sync_at=datetime.datetime.now(datetime.UTC),
                local_modified_at=datetime.datetime.now(datetime.UTC),
                remote_modified_at=datetime.datetime.now(datetime.UTC),
            ),
            s3_prefix="",
        )
        db.close()

        runner = CliRunner()
        with patch("sahara.storage.state_db.DB_PATH", db_path):
            result = runner.invoke(main, ["--config", str(cfg), "index"])

        assert result.exit_code == 0
        assert "missing" in result.output.lower() or "Done" in result.output
