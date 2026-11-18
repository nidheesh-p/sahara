"""Hybrid retrieval helpers: Reciprocal Rank Fusion, safe FTS5 queries and
result confidence labels."""

from __future__ import annotations

import math
from collections.abc import Collection, Hashable, Mapping, Sequence
from typing import TypeVar

__all__ = [
    "CONFIDENCE_LEVELS",
    "RRF_K",
    "build_fts_query",
    "confidence_label",
    "query_terms",
    "reciprocal_rank_fusion",
    "term_coverage",
    "term_weight",
]

RRF_K = 60
CONFIDENCE_LEVELS = ("high", "medium", "low")
# Confidence thresholds (see confidence_label), each set midway between the
# answerable and not_present signals of the non-holdout eval queries.
HIGH_COVERAGE = 0.5
MEDIUM_COVERAGE = 0.4
AGREEMENT_COVERAGE_WEIGHT = 0.5
AGREEMENT_SCORE = 0.70
# Vector score alone: not_present queries reach 0.67, so only clearly higher counts.
HIGH_VECTOR_SCORE = 0.80
MEDIUM_VECTOR_SCORE = 0.72

K = TypeVar("K", bound=Hashable)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[K]], k: int = RRF_K
) -> list[tuple[K, float]]:
    """Fuse ranked key lists with Reciprocal Rank Fusion.

    Each key scores ``sum(1 / (k + rank))`` over the rankings it appears in,
    with 1-based ranks; a repeated key within one ranking counts only at its
    first position. Returns ``(key, score)`` pairs, best first; ties keep the
    order in which keys were first seen.
    """
    scores: dict[K, float] = {}
    for ranking in rankings:
        seen: set[K] = set()
        for rank, key in enumerate(ranking, start=1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    order = {key: i for i, key in enumerate(scores)}
    return sorted(scores.items(), key=lambda item: (-item[1], order[item[0]]))


def build_fts_query(text: str) -> str:
    """Turn free text into an FTS5 MATCH expression that cannot raise.

    Splits on whitespace, strips double quotes and control characters (the
    only characters that can break a quoted FTS5 string), drops terms with no
    letters or digits, double-quotes every remaining term so operators such as
    AND, OR, NOT and NEAR are matched literally, and joins them with OR.
    Returns "" when nothing searchable is left.
    """
    terms: list[str] = []
    for raw in text.split():
        term = "".join(ch for ch in raw if ch != '"' and ch.isprintable())
        if not any(ch.isalnum() for ch in term):
            continue
        quoted = f'"{term}"'
        if quoted not in terms:
            terms.append(quoted)
    return " OR ".join(terms)


def query_terms(text: str) -> list[str]:
    """Lower-cased alphanumeric runs of ``text``, first occurrence order.

    Mirrors how the FTS5 ``unicode61`` tokenizer splits text, so each term can
    be looked up in the keyword index on its own.
    """
    terms: list[str] = []
    current: list[str] = []
    for ch in text.lower() + " ":
        if ch.isalnum():
            current.append(ch)
            continue
        if current:
            term = "".join(current)
            if term not in terms:
                terms.append(term)
            current = []
    return terms


def term_weight(doc_freq: int, total_chunks: int) -> float:
    """BM25-style inverse document frequency; rare terms weigh more.

    A term found in no chunk gets the largest weight, a term found in every
    chunk a weight close to zero.
    """
    n = max(total_chunks, 0)
    df = min(max(doc_freq, 0), n)
    return math.log(1.0 + (n - df + 0.5) / (df + 0.5))


def term_coverage(weights: Mapping[str, float], present: Collection[str]) -> float:
    """Share of the query's term weight that appears in the matched chunk.

    ``weights`` maps every query term to its ``term_weight``; ``present`` holds
    the terms found in the chunk. Returns 0.0 when there are no terms.
    """
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    return sum(w for term, w in weights.items() if term in present) / total


def confidence_label(
    match: str,
    vector_score: float | None,
    keyword_rank: int | None,
    coverage: float,
) -> str:
    """Label one search result "high", "medium" or "low".

    ``match`` is "vector", "keyword" or "both"; ``vector_score`` is the file's
    best vector similarity (None when only keyword search found it);
    ``keyword_rank`` is its 1-based BM25 position among keyword hits (None when
    keyword search missed it); ``coverage`` is ``term_coverage`` for the
    matched chunk. Thresholds were tuned on the non-holdout eval queries only;
    see tests/eval/BASELINE.md.
    """
    score = vector_score or 0.0
    on_keyword_side = match in ("keyword", "both") and keyword_rank is not None
    # Most of the query's rare terms are in the chunk: an exact hit.
    if on_keyword_side and coverage >= HIGH_COVERAGE:
        return "high"
    if score >= HIGH_VECTOR_SCORE:
        return "high"
    if on_keyword_side and coverage >= MEDIUM_COVERAGE:
        return "medium"
    # Both retrievers put this file first and it shares some rare terms: a
    # paraphrase hit. Vector score alone cannot tell these from absent answers.
    if (
        match == "both"
        and keyword_rank == 1
        and score + AGREEMENT_COVERAGE_WEIGHT * coverage >= AGREEMENT_SCORE
    ):
        return "medium"
    if score >= MEDIUM_VECTOR_SCORE:
        return "medium"
    return "low"
