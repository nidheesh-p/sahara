"""EPUB text extraction tests for TextExtractor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sahara.search.search_engine import TextExtractor, _html_to_text


class TestHtmlToText:
    def test_strips_tags_and_keeps_prose(self):
        html = "<html><body><h1>Heading</h1><p>The quick brown fox.</p></body></html>"
        result = _html_to_text(html)
        assert "Heading" in result
        assert "The quick brown fox." in result
        assert "<" not in result

    def test_skips_script_and_style_content(self):
        html = (
            "<html><head><style>.a{color:red}</style></head>"
            "<body><script>var x = 1;</script><p>Visible text</p></body></html>"
        )
        result = _html_to_text(html)
        assert "Visible text" in result
        assert "color:red" not in result
        assert "var x" not in result

    def test_collapses_whitespace(self):
        html = "<p>one</p>\n\n   <p>two</p>"
        result = _html_to_text(html)
        assert "one" in result
        assert "two" in result
        assert "   " not in result
