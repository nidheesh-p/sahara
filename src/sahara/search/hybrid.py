"""Hybrid retrieval helpers: Reciprocal Rank Fusion and safe FTS5 queries."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import TypeVar

__all__ = ["RRF_K", "build_fts_query", "reciprocal_rank_fusion"]

RRF_K = 60

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
