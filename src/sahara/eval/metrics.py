"""Pure retrieval metrics used by the offline evaluation harness."""

from __future__ import annotations

from collections.abc import Collection, Sequence

__all__ = [
    "not_present_false_positive_rate",
    "recall_at_k",
    "reciprocal_rank",
]


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Fraction of ``relevant`` items that appear in the first ``k`` of ``ranked``.

    Raises ValueError when ``k`` is not positive or ``relevant`` is empty,
    because recall is undefined for a query with nothing to find.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    wanted = set(relevant)
    if not wanted:
        raise ValueError("recall is undefined for a query with no relevant items")
    hits = wanted.intersection(ranked[:k])
    return len(hits) / len(wanted)


def reciprocal_rank(ranked: Sequence[str], relevant: Collection[str]) -> float:
    """``1 / rank`` of the first relevant item in ``ranked``; 0.0 if none appears."""
    wanted = set(relevant)
    for position, item in enumerate(ranked, start=1):
        if item in wanted:
            return 1.0 / position
    return 0.0


def not_present_false_positive_rate(
    scores_per_query: Sequence[Sequence[float]],
    threshold: float,
) -> float:
    """Share of not-present queries that returned any result scoring above ``threshold``.

    Each element of ``scores_per_query`` holds the result scores for one query
    whose answer is absent from the corpus. A query counts as one false positive
    when at least one of its scores is strictly greater than ``threshold``.
    Returns 0.0 when there are no queries.
    """
    if not scores_per_query:
        return 0.0
    false_positives = sum(
        1 for scores in scores_per_query if any(s > threshold for s in scores)
    )
    return false_positives / len(scores_per_query)
