"""Tests for search result confidence labels and the held-out eval acceptance."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from sahara.eval.runner import load_queries, run_eval
from sahara.search.hybrid import (
    CONFIDENCE_LEVELS,
    confidence_label,
    query_terms,
    term_coverage,
    term_weight,
)
from sahara.search.search_engine import SearchEngine
from sahara.storage.state_db import StateDB
from tests.test_hybrid_retrieval import FILES, _engine

EVAL_DIR = Path(__file__).parent / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QUERIES_PATH = EVAL_DIR / "queries.json"
# Hybrid recall@5 on the original (non-holdout) answerable queries; see BASELINE.md.
HYBRID_RECALL_AT_5 = 1.0


# --- pure scoring --------------------------------------------------------------


def test_query_terms_follow_fts_tokenisation() -> None:
    assert query_terms("INV-2047 invoice") == ["inv", "2047", "invoice"]
    assert query_terms("$18,000 a A a") == ["18", "000", "a"]
    assert query_terms("  ") == []
    assert query_terms('O\'Brien "NEAR"') == ["o", "brien", "near"]


def test_term_weight_favours_rare_terms() -> None:
    assert term_weight(0, 10) == pytest.approx(math.log(1 + 10.5 / 0.5))
    assert term_weight(10, 10) == pytest.approx(math.log(1 + 0.5 / 10.5))
    assert term_weight(0, 10) > term_weight(1, 10) > term_weight(9, 10)
    assert term_weight(50, 10) == term_weight(10, 10)  # df clamped to corpus size
    assert term_weight(0, 0) > 0


def test_term_coverage_is_weighted_share() -> None:
    weights = {"rare": 3.0, "common": 1.0}
    assert term_coverage(weights, {"rare"}) == 0.75
    assert term_coverage(weights, {"common"}) == 0.25
    assert term_coverage(weights, {"rare", "common", "extra"}) == 1.0
    assert term_coverage(weights, set()) == 0.0
    assert term_coverage({}, {"x"}) == 0.0


@pytest.mark.parametrize(
    ("match", "score", "rank", "coverage", "expected"),
    [
        ("both", 0.48, 1, 1.0, "high"),       # exact identifier, weak vector score
        ("keyword", None, 1, 0.5, "high"),    # keyword-only exact hit
        ("both", 0.60, 2, 0.45, "medium"),    # most rare terms present
        ("both", 0.64, 1, 0.20, "medium"),    # both retrievers agree: paraphrase
        ("both", 0.64, 2, 0.20, "low"),       # agreement needs keyword rank 1
        ("both", 0.62, 1, 0.13, "low"),       # agreement score below threshold
        ("vector", 0.67, None, 0.0, "low"),   # vector alone in the ambiguous band
        ("vector", 0.73, None, 0.0, "medium"),
        ("vector", 0.85, None, 0.0, "high"),
        ("keyword", None, 3, 0.3, "low"),
        ("vector", 0.30, None, 0.9, "low"),   # coverage counts only for keyword hits
    ],
)
def test_confidence_label_cases(
    match: str, score: float | None, rank: int | None, coverage: float, expected: str
) -> None:
    assert confidence_label(match, score, rank, coverage) == expected


# --- SearchEngine labels -----------------------------------------------------------


def test_search_labels_without_reordering(tmp_path: Path) -> None:
    with StateDB(tmp_path / "state.db") as db:
        engine = _engine(db, tmp_path, FILES)
        hybrid = engine.search("INV-2047 invoice", top_k=3)
        assert {r["confidence"] for r in hybrid} <= set(CONFIDENCE_LEVELS)
        rrf = [r["rrf_score"] for r in hybrid]
        assert rrf == sorted(rrf, reverse=True)
        assert hybrid[0]["relative_path"] == "kestrel.txt"
        assert hybrid[0]["confidence"] == "high"

        with pytest.MonkeyPatch.context() as mp:  # labels never change fusion output
            mp.setattr(engine, "_label_confidence", lambda *args, **kwargs: None)
            unlabelled = engine.search("INV-2047 invoice", top_k=3)
        assert [{k: v for k, v in r.items() if k != "confidence"} for r in hybrid] == unlabelled

        keyword = engine.search("INV-2047", top_k=3, mode="keyword")
        assert keyword[0]["match"] == "keyword" and keyword[0]["confidence"] == "high"
        absent = engine.search("tarragon plumbing", top_k=3)
        assert absent and {r["confidence"] for r in absent} == {"low"}


# --- eval acceptance (real model, real corpus; must never skip) ----------------------


@pytest.fixture(scope="module")
def eval_runs() -> dict[str, dict]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HF_HUB_OFFLINE", "1")
        labelled = run_eval(CORPUS_DIR, QUERIES_PATH, k=5, mode="hybrid")
        mp.setattr(SearchEngine, "_label_confidence", lambda *args, **kwargs: None)
        unlabelled = run_eval(CORPUS_DIR, QUERIES_PATH, k=5, mode="hybrid")
    return {"labelled": labelled, "unlabelled": unlabelled}


def test_holdout_is_large_enough() -> None:
    held = [q for q in load_queries(QUERIES_PATH) if q.holdout]
    assert sum(q.category == "not_present" for q in held) >= 10
    assert sum(q.category != "not_present" for q in held) >= 8


def test_holdout_not_present_rarely_confident(eval_runs: dict[str, dict]) -> None:
    holdout = eval_runs["labelled"]["holdout"]
    assert holdout["categories"]["not_present"]["count"] >= 10
    assert holdout["not_present_confident_fp_rate"] <= 0.35


def test_holdout_answerable_confident_top1(eval_runs: dict[str, dict]) -> None:
    assert eval_runs["labelled"]["holdout"]["confident_top1_rate"] >= 0.80


def test_original_recall_unchanged(eval_runs: dict[str, dict]) -> None:
    labelled, unlabelled = eval_runs["labelled"], eval_runs["unlabelled"]
    assert labelled["overall"]["recall_at_k"] == HYBRID_RECALL_AT_5
    assert [r["ranked"] for r in labelled["queries"]] == [
        r["ranked"] for r in unlabelled["queries"]
    ]
    # The score-threshold metric is still reported unchanged alongside.
    assert labelled["overall"]["not_present_fp_rate"] == unlabelled["overall"]["not_present_fp_rate"]
