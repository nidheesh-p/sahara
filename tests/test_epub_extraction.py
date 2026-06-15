"""EPUB text extraction tests for TextExtractor."""

from __future__ import annotations

import zipfile
from pathlib import Path

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


_CONTAINER_XML = (
    '<?xml version="1.0"?>'
    '<container version="1.0"'
    ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    "<rootfiles>"
    '<rootfile full-path="OEBPS/content.opf"'
    ' media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)


def _make_epub(path: Path, *, items, spine) -> None:
    """Author a minimal valid EPUB at ``path`` using only the standard library.

    ``items`` is a list of ``(item_id, filename, media_type, properties,
    content)`` tuples written under ``OEBPS/``; ``content`` of ``None`` declares
    a manifest entry without a backing file. ``spine`` is the list of item ids in
    reading order. No third-party (copyleft) writer is involved.
    """
    manifest = "".join(
        f'<item id="{item_id}" href="{filename}" media-type="{media_type}"'
        + (f' properties="{properties}"' if properties else "")
        + "/>"
        for item_id, filename, media_type, properties, _ in items
    )
    spine_xml = "".join(f'<itemref idref="{idref}"/>' for idref in spine)
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">test-id-123</dc:identifier>'
        "<dc:title>Test Book</dc:title><dc:language>en</dc:language>"
        "</metadata>"
        f"<manifest>{manifest}</manifest>"
        f"<spine>{spine_xml}</spine>"
        "</package>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf)
        for _id, filename, _media, _props, content in items:
            if content is not None:
                data = content if isinstance(content, bytes) else content.encode("utf-8")
                zf.writestr(f"OEBPS/{filename}", data)


def _chapter(body_html: str) -> str:
    return f"<html><body>{body_html}</body></html>"


class TestExtractEpub:
    def test_extracts_chapter_prose_and_skips_nav(self, tmp_path):
        f = tmp_path / "book.epub"
        _make_epub(
            f,
            items=[
                ("nav", "nav.xhtml", "application/xhtml+xml", "nav",
                 _chapter("<h1>NAVSENTINEL</h1>")),
                ("chap1", "chap_01.xhtml", "application/xhtml+xml", None,
                 _chapter("<h1>Heading</h1><p>QUICKBROWNFOX prose.</p>")),
            ],
            spine=["nav", "chap1"],
        )
        result = TextExtractor().extract(f)
        assert result is not None
        assert "QUICKBROWNFOX" in result
        assert "NAVSENTINEL" not in result

    def test_empty_epub_returns_none(self, tmp_path):
        f = tmp_path / "empty.epub"
        _make_epub(
            f,
            items=[("chap1", "chap_01.xhtml", "application/xhtml+xml", None,
                    _chapter("<p></p>"))],
            spine=["chap1"],
        )
        assert TextExtractor().extract(f) is None

    def test_malformed_epub_returns_none(self, tmp_path):
        f = tmp_path / "broken.epub"
        f.write_bytes(b"this is not a valid epub zip")
        assert TextExtractor().extract(f) is None

    def test_missing_container_returns_none(self, tmp_path):
        # A valid ZIP that lacks META-INF/container.xml is not a usable EPUB.
        f = tmp_path / "no_container.epub"
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
        assert TextExtractor().extract(f) is None

    def test_resolves_hrefs_relative_to_opf_directory(self, tmp_path):
        # Document lives in a subdirectory; href is relative to the OPF (OEBPS/).
        f = tmp_path / "nested.epub"
        _make_epub(
            f,
            items=[("chap1", "text/chap_01.xhtml", "application/xhtml+xml", None,
                    _chapter("<p>NESTEDPROSE</p>"))],
            spine=["chap1"],
        )
        result = TextExtractor().extract(f)
        assert result is not None
        assert "NESTEDPROSE" in result

    def test_appends_documents_absent_from_spine(self, tmp_path):
        # Manifest documents not listed in the spine must still be extracted.
        f = tmp_path / "orphan.epub"
        _make_epub(
            f,
            items=[
                ("chap1", "chap_01.xhtml", "application/xhtml+xml", None,
                 _chapter("<p>INSPINE</p>")),
                ("chap2", "chap_02.xhtml", "application/xhtml+xml", None,
                 _chapter("<p>OFFSPINE</p>")),
            ],
            spine=["chap1"],
        )
        result = TextExtractor().extract(f)
        assert result is not None
        assert "INSPINE" in result and "OFFSPINE" in result

    def test_extracts_chapters_in_spine_order(self, tmp_path):
        """Chapters are emitted in spine (reading) order, not manifest order."""
        f = tmp_path / "ordered.epub"
        # Manifest order is [second, first]; spine (reading) order is the
        # reverse, so only spine-ordered extraction yields ALPHA before OMEGA.
        _make_epub(
            f,
            items=[
                ("second", "a_second.xhtml", "application/xhtml+xml", None,
                 _chapter("<p>OMEGA</p>")),
                ("first", "z_first.xhtml", "application/xhtml+xml", None,
                 _chapter("<p>ALPHA</p>")),
            ],
            spine=["first", "second"],
        )
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
