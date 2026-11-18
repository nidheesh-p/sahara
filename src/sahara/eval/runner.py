"""Offline retrieval evaluation: index a golden corpus and score golden queries."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any

from sahara.eval.metrics import (
    not_present_false_positive_rate,
    recall_at_k,
    reciprocal_rank,
)
from sahara.search.search_engine import SearchEngine
from sahara.storage.state_db import StateDB

__all__ = ["CATEGORIES", "DEFAULT_THRESHOLD", "EvalQuery", "load_queries", "run_eval"]

CATEGORIES = ("exact_token", "paraphrase", "date_number", "not_present")
NOT_PRESENT = "not_present"
# Similarity score above which a hit for a not_present query counts as a false positive.
DEFAULT_THRESHOLD = 0.5
_STORAGE_PREFIX = "eval"


@dataclass(frozen=True)
class EvalQuery:
    id: str
    category: str
    query: str
    relevant: tuple[str, ...]
    # Held-out queries are never used to tune search; see tests/eval/BASELINE.md.
    holdout: bool = False


def load_queries(queries_path: Path) -> list[EvalQuery]:
    """Load and validate the golden query file.

    An optional ``"holdout": true`` marks a query kept out of tuning.
    """
    queries = []
    for raw in json.loads(queries_path.read_text(encoding="utf-8")):
        holdout = raw.get("holdout", False)
        if not isinstance(holdout, bool):
            raise ValueError(f"{raw['id']}: holdout must be true or false")
        query = EvalQuery(
            raw["id"], raw["category"], raw["query"], tuple(raw["relevant"]), holdout
        )
        if query.category not in CATEGORIES:
            raise ValueError(f"{query.id}: unknown category {query.category!r}")
        if (query.category == NOT_PRESENT) != (not query.relevant):
            raise ValueError(f"{query.id}: only not_present queries may have no relevant files")
        queries.append(query)
    return queries


def _index_corpus(engine: SearchEngine, corpus_dir: Path) -> list[str]:
    indexed = []
    for path in sorted(p for p in corpus_dir.iterdir() if p.is_file()):
        result = engine.index_file_with_result(path, _STORAGE_PREFIX, path.name, force=True)
        if not result.indexed:
            raise RuntimeError(f"corpus file {path.name} was not indexed: {result.reason}")
        indexed.append(path.name)
    return indexed


def _answerable_summary(rows: Sequence[dict[str, Any]]) -> dict[str, float]:
    return {
        "recall_at_k": fmean(r["recall_at_k"] for r in rows) if rows else 0.0,
        "mrr": fmean(r["reciprocal_rank"] for r in rows) if rows else 0.0,
    }


def run_eval(
    corpus_dir: Path,
    queries_path: Path,
    k: int = 5,
    threshold: float = DEFAULT_THRESHOLD,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Index ``corpus_dir`` into a temporary database and score every query.

    ``mode`` is passed to ``SearchEngine.search`` ("hybrid", "vector" or
    "keyword"). Recall and reciprocal rank use the top ``k`` results (MRR@k).
    Returns a dict with ``overall`` and per-``categories`` numbers for the
    non-holdout queries, the same numbers for held-out queries under
    ``holdout``, and per-query rows. ``not_present_confident_fp_rate`` is the
    share of not_present queries whose top result is labelled "high" or
    "medium"; ``confident_top1_rate`` the share of answerable queries whose top
    result is relevant and labelled so.
    """
    queries = load_queries(queries_path)
    with tempfile.TemporaryDirectory(prefix="sahara-eval-") as tmp:
        db = StateDB(Path(tmp) / "state.db").connect()
        try:
            engine = SearchEngine(db)
            indexed = _index_corpus(engine, corpus_dir)
            vec_backend = db.has_vec_table()
            fts_backend = db.has_fts_table()
            rows = []
            for q in queries:
                results = engine.search(
                    q.query, top_k=k, storage_prefix=_STORAGE_PREFIX, mode=mode
                )
                ranked = [r["relative_path"] for r in results]
                confidence = [r.get("confidence", "low") for r in results]
                row: dict[str, Any] = {
                    "id": q.id,
                    "category": q.category,
                    "holdout": q.holdout,
                    "ranked": ranked,
                    "scores": [r["score"] for r in results],
                    "confidence": confidence,
                    "match": [r.get("match", "vector") for r in results],
                }
                if q.category != NOT_PRESENT:
                    row["recall_at_k"] = recall_at_k(ranked, q.relevant, k)
                    row["reciprocal_rank"] = reciprocal_rank(ranked, q.relevant)
                    row["confident_top1"] = bool(ranked) and (
                        ranked[0] in q.relevant and confidence[0] in ("high", "medium")
                    )
                rows.append(row)
        finally:
            db.close()

    tuning = _summarise([r for r in rows if not r["holdout"]], threshold)
    return {
        "k": k,
        "threshold": threshold,
        "mode": mode,
        "vec_backend": vec_backend,
        "fts_backend": fts_backend,
        "corpus_files": len(indexed),
        "num_queries": len(rows),
        **tuning,
        "holdout": _summarise([r for r in rows if r["holdout"]], threshold),
        "queries": rows,
    }


def _confident_fp_rate(rows: Sequence[dict[str, Any]]) -> float:
    """Share of not_present rows whose top result is labelled high or medium."""
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["confidence"][:1] in (["high"], ["medium"])) / len(rows)


def _confident_top1_rate(rows: Sequence[dict[str, Any]]) -> float:
    """Share of answerable rows whose top result is relevant and high or medium."""
    if not rows:
        return 0.0
    return sum(1 for r in rows if r["confident_top1"]) / len(rows)


def _summarise(rows: Sequence[dict[str, Any]], threshold: float) -> dict[str, Any]:
    """``overall``, per-``categories`` and confidence numbers for ``rows``."""
    answerable = [r for r in rows if r["category"] != NOT_PRESENT]
    absent = [r for r in rows if r["category"] == NOT_PRESENT]
    absent_scores = [r["scores"] for r in absent]
    categories: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        members = [r for r in rows if r["category"] == category]
        if category == NOT_PRESENT:
            stats: dict[str, Any] = {
                "fp_rate": not_present_false_positive_rate(absent_scores, threshold),
                "confident_fp_rate": _confident_fp_rate(members),
            }
        else:
            stats = {
                **_answerable_summary(members),
                "confident_top1_rate": _confident_top1_rate(members),
            }
        categories[category] = {"count": len(members), **stats}
    return {
        "overall": {
            **_answerable_summary(answerable),
            "not_present_fp_rate": not_present_false_positive_rate(absent_scores, threshold),
        },
        "categories": categories,
        # Not in "overall" so the score-threshold metric keeps its exact shape.
        "not_present_confident_fp_rate": _confident_fp_rate(absent),
        "confident_top1_rate": _confident_top1_rate(answerable),
    }
