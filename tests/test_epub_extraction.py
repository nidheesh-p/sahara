"""EPUB text extraction tests for TextExtractor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sahara.search.search_engine import TextExtractor, _decode_xhtml, _html_to_text


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

    def test_separates_adjacent_block_elements(self):
        assert _html_to_text("<p>one</p><p>two</p>") == "one two"

    def test_does_not_split_inline_elements(self):
        assert _html_to_text("wor<b>d</b>s") == "words"

    def test_separates_headings_and_list_items(self):
        html = "<h1>Title</h1><ul><li>alpha</li><li>beta</li></ul>"
        assert _html_to_text(html) == "Title alpha beta"


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

    def test_extracts_chapters_in_spine_order(self, tmp_path):
        """Chapters are emitted in spine (reading) order, not manifest order."""
        epub = pytest.importorskip("ebooklib.epub")
        book = epub.EpubBook()
        book.set_identifier("order-id")
        book.set_title("Ordered Book")
        book.set_language("en")

        first = epub.EpubHtml(title="First", file_name="z_first.xhtml", lang="en")
        first.content = "<html><body><p>ALPHA</p></body></html>"
        second = epub.EpubHtml(title="Second", file_name="a_second.xhtml", lang="en")
        second.content = "<html><body><p>OMEGA</p></body></html>"

        # Manifest/add order is [second, first]; spine (reading) order is the
        # reverse, so only spine-ordered extraction yields ALPHA before OMEGA.
        book.add_item(second)
        book.add_item(first)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.toc = (first, second)
        book.spine = ["nav", first, second]

        f = tmp_path / "ordered.epub"
        epub.write_epub(str(f), book)

        result = TextExtractor().extract(f)
        assert result is not None
        assert "ALPHA" in result and "OMEGA" in result
        assert result.index("ALPHA") < result.index("OMEGA")


class TestDecodeXhtml:
    def test_decodes_utf8(self):
        assert _decode_xhtml("hello café".encode()) == "hello café"

    def test_decodes_utf16_with_bom(self):
        # str.encode("utf-16") prepends a byte-order mark.
        raw = "café résumé".encode("utf-16")
        assert _decode_xhtml(raw) == "café résumé"

    def test_decodes_utf8_bom(self):
        assert _decode_xhtml("plain".encode("utf-8-sig")) == "plain"

    def test_invalid_bytes_fall_back_without_raising(self):
        # Not valid UTF-8 and no clean BOM: must degrade, not crash.
        result = _decode_xhtml(b"caf\xe9 text")  # latin-1 'é'
        assert isinstance(result, str)
        assert "text" in result
