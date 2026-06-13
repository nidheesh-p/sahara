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


def _build_epub(path: Path, *, chapter_title: str, body_html: str) -> None:
    """Author a minimal valid EPUB at `path` using ebooklib's writer.

    The chapter title becomes the nav/TOC link text; body_html is the
    chapter's document content. Used to exercise real ebooklib parsing.
    """
    epub = pytest.importorskip("ebooklib.epub")
    book = epub.EpubBook()
    book.set_identifier("test-id-123")
    book.set_title("Test Book")
    book.set_language("en")

    chapter = epub.EpubHtml(title=chapter_title, file_name="chap_01.xhtml", lang="en")
    chapter.content = f"<html><body>{body_html}</body></html>"
    book.add_item(chapter)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (chapter,)
    book.spine = ["nav", chapter]
    epub.write_epub(str(path), book)


class TestExtractEpub:
    def test_extracts_chapter_prose_and_skips_nav(self, tmp_path):
        f = tmp_path / "book.epub"
        _build_epub(
            f,
            chapter_title="NAVSENTINEL",
            body_html="<h1>Heading</h1><p>QUICKBROWNFOX prose.</p>",
        )
        result = TextExtractor().extract(f)
        assert result is not None
        assert "QUICKBROWNFOX" in result
        assert "NAVSENTINEL" not in result

    def test_empty_epub_returns_none(self, tmp_path):
        f = tmp_path / "empty.epub"
        _build_epub(f, chapter_title="Empty", body_html="<p></p>")
        assert TextExtractor().extract(f) is None

    def test_malformed_epub_returns_none(self, tmp_path):
        f = tmp_path / "broken.epub"
        f.write_bytes(b"this is not a valid epub zip")
        assert TextExtractor().extract(f) is None

    def test_missing_ebooklib_returns_none(self, tmp_path):
        f = tmp_path / "book.epub"
        f.write_bytes(b"PK\x03\x04 fake")
        extractor = TextExtractor()
        with patch("builtins.__import__", side_effect=lambda name, *a, **kw: (
            (_ for _ in ()).throw(ImportError("No module named 'ebooklib'"))
            if name == "ebooklib" else __import__(name, *a, **kw)
        )):
            result = extractor._extract_epub(f)
        assert result is None
