"""Tests for the offline retrieval evaluation harness (sahara.eval)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from sahara.eval.metrics import (
    not_present_false_positive_rate,
    recall_at_k,
    reciprocal_rank,
)
from sahara.eval.runner import CATEGORIES, load_queries, run_eval

EVAL_DIR = Path(__file__).parent / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QUERIES_PATH = EVAL_DIR / "queries.json"
SCAN_PNG = "scan_lab_results_fictional.png"
SCAN_PDF = "scan_loan_letter_fictional.pdf"
# Recorded floor for overall recall@5 of vector-only search; see tests/eval/BASELINE.md.
RECALL_AT_5_FLOOR = 0.90


# --- metrics -------------------------------------------------------------


def test_recall_at_k_hand_computed() -> None:
    ranked = ["a", "b", "c", "d", "e"]
    assert recall_at_k(ranked, {"a"}, 1) == 1.0
    assert recall_at_k(ranked, {"c"}, 2) == 0.0
    assert recall_at_k(ranked, {"b", "d", "z"}, 5) == pytest.approx(2 / 3)
    assert recall_at_k(ranked, {"b", "e"}, 3) == 0.5
    assert recall_at_k(["a"], {"a", "b"}, 10) == 0.5


def test_recall_at_k_rejects_undefined_inputs() -> None:
    with pytest.raises(ValueError):
        recall_at_k(["a"], set(), 5)
    with pytest.raises(ValueError):
        recall_at_k(["a"], {"a"}, 0)


def test_reciprocal_rank_hand_computed() -> None:
    assert reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0
    assert reciprocal_rank(["a", "b", "c"], {"c"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(["a", "b", "c", "d"], {"d", "b"}) == 0.5
    assert reciprocal_rank(["a", "b"], {"z"}) == 0.0
    assert reciprocal_rank([], {"a"}) == 0.0


def test_not_present_false_positive_rate_hand_computed() -> None:
    scores = [[0.9, 0.2], [0.4, 0.3], [], [0.5]]
    # Only the first query has a score strictly above 0.5.
    assert not_present_false_positive_rate(scores, 0.5) == 0.25
    assert not_present_false_positive_rate(scores, 0.35) == 0.75
    assert not_present_false_positive_rate(scores, 0.95) == 0.0
    assert not_present_false_positive_rate([], 0.5) == 0.0


# --- golden data -----------------------------------------------------------


def test_golden_queries_are_well_formed() -> None:
    queries = load_queries(QUERIES_PATH)
    corpus_names = {p.name for p in CORPUS_DIR.iterdir() if p.is_file()}
    assert 15 <= len(corpus_names) <= 25
    assert len(queries) >= 30
    assert {q.category for q in queries} == set(CATEGORIES)
    assert len({q.id for q in queries}) == len(queries)
    for q in queries:
        assert set(q.relevant) <= corpus_names, q.id


def test_load_queries_rejects_unknown_category(tmp_path: Path) -> None:
    bad = tmp_path / "queries.json"
    bad.write_text(json.dumps([
        {"id": "x", "category": "fuzzy", "query": "q", "relevant": ["a.txt"]}
    ]))
    with pytest.raises(ValueError, match="unknown category"):
        load_queries(bad)


def test_load_queries_reads_requires_ocr(tmp_path: Path) -> None:
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([
        {"id": "a", "category": "exact_token", "query": "q", "relevant": ["a.png"],
         "requires_ocr": True},
        {"id": "b", "category": "exact_token", "query": "q", "relevant": ["b.txt"]},
    ]))
    assert [q.requires_ocr for q in load_queries(path)] == [True, False]
    path.write_text(json.dumps([
        {"id": "c", "category": "exact_token", "query": "q", "relevant": ["a.png"],
         "requires_ocr": "yes"},
    ]))
    with pytest.raises(ValueError, match="requires_ocr"):
        load_queries(path)


def test_scanned_fixtures_have_ocr_only_queries() -> None:
    ocr = [q for q in load_queries(QUERIES_PATH) if q.requires_ocr]
    answerable = ("exact_token", "date_number")
    png = [q for q in ocr if q.relevant == (SCAN_PNG,) and q.category in answerable]
    pdf = [q for q in ocr if q.relevant == (SCAN_PDF,) and q.category in answerable]
    assert len(png) >= 2 and len(pdf) >= 1
    for name in (SCAN_PNG, SCAN_PDF):
        assert (CORPUS_DIR / name).stat().st_size < 300 * 1024


@pytest.mark.usefixtures("offline_embedding_model")
def test_run_eval_without_ocr_backend_skips_images_and_ocr_queries() -> None:
    with patch("sahara.ocr.get_backend", return_value=None):
        result = run_eval(CORPUS_DIR, QUERIES_PATH, k=5)
    assert result["ocr_backend"] is None
    assert result["skipped_for_ocr"] == {"corpus_files": 1, "queries": 5}
    assert not any(row["requires_ocr"] for row in result["queries"])
    assert result["overall"]["recall_at_k"] >= RECALL_AT_5_FLOOR


# --- end to end ------------------------------------------------------------


@pytest.fixture
def offline_embedding_model() -> Iterator[None]:
    """Force offline model loading; skip only if the model cannot be loaded."""
    from sahara.search.search_engine import load_embedding_model

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HF_HUB_OFFLINE", "1")
        try:
            load_embedding_model()
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            pytest.skip(
                "embedding model cannot be loaded offline (install the 'search' "
                "extra and cache the model, or set FASTEMBED_CACHE_PATH): "
                f"{type(exc).__name__}: {exc}"
            )
        yield


@pytest.mark.usefixtures("offline_embedding_model")
def test_run_eval_end_to_end_meets_recall_floor() -> None:
    result = run_eval(CORPUS_DIR, QUERIES_PATH, k=5)

    assert result["k"] == 5
    skipped = result["skipped_for_ocr"]
    assert result["corpus_files"] + skipped["corpus_files"] == len(
        [p for p in CORPUS_DIR.iterdir() if p.is_file()]
    )
    assert result["num_queries"] == len(result["queries"]) >= 30
    if result["ocr_backend"] is None:
        assert skipped["queries"] == sum(q.requires_ocr for q in load_queries(QUERIES_PATH))
    else:
        assert skipped == {"corpus_files": 0, "queries": 0}
    assert set(result["categories"]) == set(CATEGORIES)
    assert set(result["overall"]) == {"recall_at_k", "mrr", "not_present_fp_rate"}
    for category, stats in result["categories"].items():
        assert stats["count"] > 0
        keys = {"fp_rate"} if category == "not_present" else {"recall_at_k", "mrr"}
        assert keys <= set(stats)
        for key in keys:
            assert 0.0 <= stats[key] <= 1.0
    assert 0.0 <= result["overall"]["not_present_fp_rate"] <= 1.0
    assert result["overall"]["recall_at_k"] >= RECALL_AT_5_FLOOR
