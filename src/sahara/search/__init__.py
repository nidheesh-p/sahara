"""Search layer — text extraction, embeddings, semantic search.

Canonical import paths:
    from sahara.search import SearchEngine, TextExtractor
    from sahara.search.search_engine import SearchEngine
"""

from sahara.search.search_engine import SearchEngine, TextExtractor  # noqa: F401

__all__ = ["SearchEngine", "TextExtractor"]
