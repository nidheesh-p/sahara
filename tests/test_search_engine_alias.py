"""Tests for the legacy sahara.search_engine module alias."""

from __future__ import annotations

import importlib
import sys


def test_search_engine_alias_points_at_canonical_module() -> None:
    alias = importlib.import_module("sahara.search_engine")
    canonical = importlib.import_module("sahara.search.search_engine")

    assert alias is canonical
    assert sys.modules["sahara.search_engine"] is canonical
