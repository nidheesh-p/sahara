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


def load_queries(queries_path: Path) -> list[EvalQuery]:
    """Load and validate the golden query file."""
    queries = []
    for raw in json.loads(queries_path.read_text(encoding="utf-8")):
        query = EvalQuery(raw["id"], raw["category"], raw["query"], tuple(raw["relevant"]))
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
    Returns a dict with ``overall`` and per-``categories`` numbers plus
    per-query rows.
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
                row: dict[str, Any] = {
                    "id": q.id,
                    "category": q.category,
                    "ranked": ranked,
                    "scores": [r["score"] for r in results],
                }
                if q.category != NOT_PRESENT:
                    row["recall_at_k"] = recall_at_k(ranked, q.relevant, k)
                    row["reciprocal_rank"] = reciprocal_rank(ranked, q.relevant)
                rows.append(row)
        finally:
            db.close()

    answerable = [r for r in rows if r["category"] != NOT_PRESENT]
    absent_scores = [r["scores"] for r in rows if r["category"] == NOT_PRESENT]
    categories: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        members = [r for r in rows if r["category"] == category]
        if category == NOT_PRESENT:
            stats: dict[str, Any] = {
                "fp_rate": not_present_false_positive_rate(absent_scores, threshold)
            }
        else:
            stats = _answerable_summary(members)
        categories[category] = {"count": len(members), **stats}
    return {
        "k": k,
        "threshold": threshold,
        "mode": mode,
        "vec_backend": vec_backend,
        "fts_backend": fts_backend,
        "corpus_files": len(indexed),
        "num_queries": len(rows),
        "overall": {
            **_answerable_summary(answerable),
            "not_present_fp_rate": not_present_false_positive_rate(absent_scores, threshold),
        },
        "categories": categories,
        "queries": rows,
    }
