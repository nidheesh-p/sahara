"""Tests for hybrid retrieval: FTS5 keyword index, RRF fusion and search modes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import sahara.storage.state_db as state_db_module
from sahara.eval.runner import load_queries, run_eval
from sahara.search.hybrid import build_fts_query, reciprocal_rank_fusion
from sahara.search.search_engine import SearchEngine
from sahara.storage.state_db import StateDB

EVAL_DIR = Path(__file__).parent / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QUERIES_PATH = EVAL_DIR / "queries.json"

HOSTILE_INPUTS = [
    "INV-2047", "$18,000", "2025-07-05", "O'Brien", '"', "'", "AND", "OR", "NOT",
    "NEAR", "NEAR(a b)", "a AND NEAR", "", "   ", "*", "^", "col:value", "(", ")",
    '"unterminated', "a\x00b", "-", "+", "{x}", "ünïcödé",
]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[StateDB]:
    database = StateDB(tmp_path / "state.db").connect()
    yield database
    database.close()


def _fake_embed(texts: list[str]) -> list[Any]:
    """Deterministic bag-of-words embedding so tests need no model."""
    vectors = []
    for text in texts:
        vec = np.zeros(384, dtype=np.float32)
        for word in text.lower().split():
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 384] += 1.0
        vectors.append(vec)
    return vectors


def _engine(db: StateDB, tmp_path: Path, files: dict[str, str], prefix: str = "p") -> SearchEngine:
    engine = SearchEngine(db)
    engine._embed = _fake_embed  # type: ignore[method-assign]
    for name, text in files.items():
        path = tmp_path / prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        assert engine.index_file(path, prefix, name, force=True)
    return engine


def _paths(rows: list[dict]) -> list[str]:
    return [r["relative_path"] for r in rows]


# --- reciprocal_rank_fusion --------------------------------------------------


def test_rrf_hand_computed() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "d"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 62)
    assert fused["d"] == pytest.approx(1 / 63)
    order = [key for key, _ in reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "d"]])]
    assert order == ["b", "c", "a", "d"]


def test_rrf_small_k_duplicates_and_ties() -> None:
    assert reciprocal_rank_fusion([["x", "y", "x"]], k=0) == [("x", 1.0), ("y", 0.5)]
    # Tied scores keep first-seen order.
    assert [key for key, _ in reciprocal_rank_fusion([["p"], ["q"]])] == ["p", "q"]
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


# --- build_fts_query -----------------------------------------------------------


def test_build_fts_query_quotes_and_ors_terms() -> None:
    assert build_fts_query("INV-2047") == '"INV-2047"'
    assert build_fts_query("annual fee $18,000") == '"annual" OR "fee" OR "$18,000"'
    assert build_fts_query("O'Brien AND NEAR") == '"O\'Brien" OR "AND" OR "NEAR"'
    assert build_fts_query('say "hi" twice "') == '"say" OR "hi" OR "twice"'
    assert build_fts_query("") == ""
    assert build_fts_query('"') == ""
    assert build_fts_query("a a A") == '"a" OR "A"'


@pytest.mark.parametrize("text", HOSTILE_INPUTS)
def test_build_fts_query_never_breaks_fts5(db: StateDB, text: str) -> None:
    db.upsert_chunk("p", "doc.txt", 0, "h", "INV-2047 costs $18,000 on 2025-07-05 for O'Brien")
    query = build_fts_query(text)
    assert '"' not in query.replace('" OR "', "").strip('"')
    if query:
        # Executed directly so an FTS5 syntax error would raise here.
        db.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (query,)).fetchall()
    assert isinstance(db.fts_keyword_search(query, k=5), list)


def test_hostile_identifiers_match_their_chunk(db: StateDB) -> None:
    db.upsert_chunk("p", "doc.txt", 0, "h", "INV-2047 costs $18,000 on 2025-07-05 for O'Brien")
    db.upsert_chunk("p", "other.txt", 0, "h", "nothing relevant here")
    for text in ("INV-2047", "$18,000", "2025-07-05", "O'Brien"):
        assert _paths(db.fts_keyword_search(build_fts_query(text), k=5)) == ["doc.txt"], text


# --- migration and trigger sync --------------------------------------------------


def _kw(db: StateDB, text: str, **filters: Any) -> list[str]:
    return _paths(db.fts_keyword_search(build_fts_query(text), k=50, **filters))


def test_migration_creates_index_and_triggers_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with StateDB(path) as first:
        assert first.has_fts_table()
        names = {r[0] for r in first.conn.execute("SELECT name FROM sqlite_master")}
        assert {"chunks_fts", "chunks_fts_ai", "chunks_fts_ad", "chunks_fts_au"} <= names
        assert first.get_config_value("schema_v5_fts") == "1"
        first.upsert_chunk("p", "a.txt", 0, "h", "kestrel invoice")
    with StateDB(path) as second:  # reconnect: migration is a no-op
        assert second.has_fts_table()
        assert _kw(second, "kestrel") == ["a.txt"]


def test_migration_backfills_existing_chunks(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with StateDB(path) as old:  # simulate a pre-FTS database holding chunks
        old.conn.executescript(
            "DROP TRIGGER chunks_fts_ai; DROP TRIGGER chunks_fts_ad; "
            "DROP TRIGGER chunks_fts_au; DROP TABLE chunks_fts;"
        )
        old.delete_config_value("schema_v5_fts")
        old.upsert_chunk("p", "old.txt", 0, "h", "heron project status")
        assert not old.has_fts_table()
    with StateDB(path) as upgraded:
        assert _kw(upgraded, "heron") == ["old.txt"]


def test_triggers_sync_insert_update_delete(db: StateDB) -> None:
    db.upsert_chunk("p", "a.txt", 0, "h1", "alpha content")
    assert _kw(db, "alpha") == ["a.txt"]
    db.upsert_chunk("p", "a.txt", 0, "h2", "beta content")  # ON CONFLICT -> UPDATE
    assert _kw(db, "alpha") == []
    assert _kw(db, "beta") == ["a.txt"]
    db.delete_chunks_for_file("p", "a.txt")
    assert _kw(db, "beta") == []
    db.upsert_chunk("q", "b.txt", 0, "h", "gamma")
    db.delete_index_for_prefix("q")
    assert _kw(db, "gamma") == []


def test_keyword_search_honours_filters(db: StateDB) -> None:
    db.upsert_chunk("p", "a.txt", 0, "h", "shared token")
    db.upsert_chunk("p", "b.txt", 0, "h", "shared token")
    db.upsert_chunk("q", "a.txt", 0, "h", "shared token")
    assert sorted(_kw(db, "shared")) == ["a.txt", "a.txt", "b.txt"]
    assert sorted(_kw(db, "shared", storage_prefix="p")) == ["a.txt", "b.txt"]
    assert _kw(db, "shared", storage_prefix="q") == ["a.txt"]
    assert _kw(db, "shared", storage_prefix="p", candidate_paths={"b.txt"}) == ["b.txt"]
    assert _kw(db, "shared", candidate_paths=set()) == []
    assert db.fts_keyword_search("", k=5) == []
    rows = db.fts_keyword_search(build_fts_query("shared"), k=1)
    assert len(rows) == 1 and {"id", "rank", "storage_prefix", "relative_path"} <= set(rows[0])


def test_fts5_missing_degrades_to_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_db_module, "_fts5_available", lambda conn: False)
    with StateDB(tmp_path / "state.db") as db:
        assert not db.has_fts_table()
        files = {"a.txt": "kestrel invoice total", "b.txt": "garden journal tomatoes"}
        engine = _engine(db, tmp_path, files)
        assert db.fts_keyword_search(build_fts_query("kestrel"), k=5) == []
        hybrid = engine.search("kestrel invoice", top_k=2)
        vector = engine.search("kestrel invoice", top_k=2, mode="vector")
        assert _paths(hybrid) == _paths(vector)
        assert [r["score"] for r in hybrid] == [r["score"] for r in vector]
        assert {r["match"] for r in hybrid} == {"vector"}
        assert engine.search("kestrel", mode="keyword") == []


# --- SearchEngine modes ------------------------------------------------------------

FILES = {
    "kestrel.txt": "Invoice INV-2047 from Kestrel Freight for hauling services",
    "brightwell.txt": "Invoice INV-2031 from Brightwell Supplies for office paper",
    "garden.txt": "Garden journal planted tomatoes and basil this spring",
}


def test_search_modes_on_cosine_backend(db: StateDB, tmp_path: Path) -> None:
    engine = _engine(db, tmp_path, FILES)
    assert not db.has_vec_table()
    keyword = engine.search("INV-2047", top_k=3, mode="keyword")
    assert _paths(keyword) == ["kestrel.txt"]
    assert keyword[0]["score"] == 0.0 and keyword[0]["match"] == "keyword"

    hybrid = engine.search("INV-2047 invoice", top_k=3)
    assert hybrid[0]["relative_path"] == "kestrel.txt"
    assert hybrid[0]["match"] == "both"
    assert {"storage_prefix", "relative_path", "score", "snippet", "rrf_score",
            "match", "local_state"} <= set(hybrid[0])
    rrf = [r["rrf_score"] for r in hybrid]
    assert rrf == sorted(rrf, reverse=True)
    assert len(_paths(hybrid)) == len(set(_paths(hybrid)))
    vector = {r["relative_path"]: r["score"] for r in engine.search("INV-2047 invoice", mode="vector")}
    for row in hybrid:  # "score" still means the vector similarity
        assert row["score"] == pytest.approx(vector.get(row["relative_path"], 0.0))

    assert engine.search("INV-2047", mode="keyword", candidate_paths={"garden.txt"}) == []
    assert engine.search("INV-2047", mode="keyword", storage_prefix="other") == []
    assert engine.search("INV-2047", candidate_paths=set()) == []
    with pytest.raises(ValueError, match="unknown search mode"):
        engine.search("x", mode="fuzzy")


def test_hybrid_on_sqlite_vec_backend(
    db: StateDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(db, tmp_path, FILES)
    chunk_ids = {
        r["relative_path"]: r["id"]
        for r in db.conn.execute("SELECT id, relative_path FROM chunks").fetchall()
    }
    calls: list[dict] = []

    def fake_knn(query_bytes: bytes, k: int, storage_prefix: str | None = None) -> list[dict]:
        calls.append({"k": k, "storage_prefix": storage_prefix})
        rows = [("brightwell.txt", 0.1), ("garden.txt", 0.4)]  # vector misses kestrel
        return [
            {"id": chunk_ids[name], "storage_prefix": "p", "relative_path": name,
             "chunk_index": 0, "chunk_text": FILES[name], "distance": dist}
            for name, dist in rows
        ]

    monkeypatch.setattr(db, "has_vec_table", lambda: True)
    monkeypatch.setattr(db, "vec_knn_search", fake_knn)
    results = engine.search("INV-2047", top_k=3, storage_prefix="p")
    assert calls and calls[0]["storage_prefix"] == "p"
    by_path = {r["relative_path"]: r for r in results}
    assert by_path["kestrel.txt"]["match"] == "keyword"
    assert by_path["kestrel.txt"]["score"] == 0.0
    assert by_path["brightwell.txt"]["score"] == pytest.approx(0.9)
    assert by_path["garden.txt"]["match"] == "vector"
    assert _paths(engine.search("INV-2047", top_k=3, mode="vector")) == ["brightwell.txt", "garden.txt"]


# --- eval acceptance (real model, real corpus; must never skip) ----------------------


@pytest.fixture(scope="module")
def eval_results() -> dict[str, dict]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HF_HUB_OFFLINE", "1")
        return {mode: run_eval(CORPUS_DIR, QUERIES_PATH, k=5, mode=mode) for mode in ("vector", "hybrid")}


def test_hybrid_beats_vector_on_exact_and_numeric(eval_results: dict[str, dict]) -> None:
    vector, hybrid = eval_results["vector"], eval_results["hybrid"]
    assert hybrid["fts_backend"] and hybrid["mode"] == "hybrid"
    gains = []
    for category in ("exact_token", "date_number"):
        v = vector["categories"][category]["recall_at_k"]
        h = hybrid["categories"][category]["recall_at_k"]
        assert h >= v, category
        gains.append(h > v)
    assert any(gains)
    para = "paraphrase"
    assert hybrid["categories"][para]["recall_at_k"] >= vector["categories"][para]["recall_at_k"]


def test_hybrid_finds_kestrel_invoice_by_identifier(eval_results: dict[str, dict]) -> None:
    inv = [q for q in load_queries(QUERIES_PATH) if "INV-2047" in q.query]
    assert inv
    rows = {r["id"]: r for r in eval_results["hybrid"]["queries"]}
    for q in inv:
        assert "invoice_inv-2047_kestrel.txt" in rows[q.id]["ranked"][:5], q.query
