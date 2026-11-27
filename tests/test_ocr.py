"""Opt-in, local-only OCR for images and scanned PDFs (GitHub #137)."""

from __future__ import annotations

import shutil
import socket
import urllib.request
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

from sahara.cli import main
from sahara.ocr import TesseractBackend, get_backend
from sahara.search.search_engine import SearchEngine, TextExtractor
from sahara.storage.state_db import StateDB


class FakeBackend:
    name = "fake"

    def __init__(self, text: str = "scanned receipt total 42", fail_on: bytes | None = None):
        self.text = text
        self.fail_on = fail_on
        self.calls = 0

    def available(self) -> bool:
        return True

    def recognize(self, image_bytes: bytes) -> str:
        self.calls += 1
        if self.fail_on is not None and self.fail_on in image_bytes:
            raise RuntimeError("backend failure")
        return self.text


def _extractor(backend: FakeBackend | None = None) -> TextExtractor:
    extractor = TextExtractor()
    extractor.configure_ocr(True, backend or FakeBackend())
    return extractor


def _image_pdf(path: Path, pages: int = 1) -> Path:
    image_mod = pytest.importorskip("PIL.Image")
    images = [image_mod.new("RGB", (64, 64), "white") for _ in range(pages)]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


def _text_pdf(path: Path) -> Path:
    stream = b"BT /F1 12 Tf 20 100 Td (Quarterly invoice) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref,
    )
    path.write_bytes(bytes(out))
    return path


def test_disabled_by_default_returns_none_as_before(tmp_path: Path) -> None:
    png = tmp_path / "scan.png"
    png.write_bytes(b"\x89PNG not really")
    extractor = TextExtractor()
    assert extractor.ocr_enabled is False
    assert extractor.extract(png) is None
    assert extractor.extract(_image_pdf(tmp_path / "scan.pdf")) is None
    assert extractor.ocr_counters.ocr_done == 0


def test_enabled_image_returns_backend_text(tmp_path: Path) -> None:
    png = tmp_path / "scan.png"
    png.write_bytes(b"image bytes")
    extractor = _extractor()
    assert extractor.extract(png) == "scanned receipt total 42"
    assert extractor.ocr_counters.ocr_done == 1


def test_image_only_pdf_falls_back_to_ocr(tmp_path: Path) -> None:
    backend = FakeBackend()
    extractor = _extractor(backend)
    assert extractor.extract(_image_pdf(tmp_path / "scan.pdf")) == backend.text
    assert backend.calls == 1


def test_text_pdf_never_calls_backend(tmp_path: Path) -> None:
    backend = FakeBackend()
    text = _extractor(backend).extract(_text_pdf(tmp_path / "text.pdf"))
    assert text is not None and "Quarterly invoice" in text
    assert backend.calls == 0


def test_oversize_image_and_page_cap_are_skipped(tmp_path: Path) -> None:
    backend = FakeBackend()
    extractor = _extractor(backend)
    extractor.OCR_MAX_IMAGE_BYTES = 3
    png = tmp_path / "big.png"
    png.write_bytes(b"0123456789")
    assert extractor.extract(png) is None
    assert backend.calls == 0
    assert extractor.ocr_counters.ocr_skipped == 1

    capped = _extractor(backend)
    capped.OCR_MAX_PDF_PAGES = 1
    assert capped.extract(_image_pdf(tmp_path / "long.pdf", pages=3)) == backend.text
    assert backend.calls == 1
    assert capped.ocr_counters.ocr_skipped == 1


def test_backend_error_on_one_file_leaves_others_indexed(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.db").connect()
    try:
        engine = SearchEngine(db)
        engine.extractor.configure_ocr(True, FakeBackend(fail_on=b"BAD"))
        for name, data in (("a.png", b"ok"), ("b.png", b"BAD"), ("c.png", b"ok")):
            (tmp_path / name).write_bytes(data)
        with patch.object(
            SearchEngine,
            "_embed",
            side_effect=lambda texts: [np.full(384, 0.5, dtype=np.float32) for _ in texts],
        ):
            results = {
                name: engine.index_file_with_result(tmp_path / name, "", name)
                for name in ("a.png", "b.png", "c.png")
            }
    finally:
        db.close()
    assert results["a.png"].indexed and results["c.png"].indexed
    assert results["b.png"].reason == "no_text"
    assert engine.extractor.ocr_counters.ocr_done == 2
    assert engine.extractor.ocr_counters.ocr_skipped == 1


def test_auto_backend_is_none_when_nothing_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sahara.ocr.shutil.which", lambda _name: None)
    assert get_backend("auto") is None
    assert get_backend("tesseract") is None
    assert get_backend("cloud") is None


def _config(tmp_path: Path, ocr: bool) -> tuple[Path, Path]:
    sync = tmp_path / "sync"
    sync.mkdir(exist_ok=True)
    (sync / "scan.png").write_bytes(b"image bytes")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'storage_mode = "none"\nsync_folder = "{sync}"\n'
        f"ocr_enabled = {str(ocr).lower()}\n"
    )
    return cfg, sync


def test_config_rejects_unknown_backend(tmp_path: Path) -> None:
    cfg, _ = _config(tmp_path, ocr=False)
    runner = CliRunner()
    bad = runner.invoke(main, ["--config", str(cfg), "config", "set", "ocr_backend", "cloud"])
    assert bad.exit_code != 0
    good = runner.invoke(main, ["--config", str(cfg), "config", "set", "ocr_backend", "tesseract"])
    assert good.exit_code == 0
    assert 'ocr_backend = "tesseract"' in cfg.read_text()


@pytest.mark.parametrize("ocr", [False, True])
def test_index_summary_line_only_when_enabled(tmp_path: Path, ocr: bool) -> None:
    cfg, _ = _config(tmp_path, ocr=ocr)
    with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"), \
         patch("sahara.ocr.get_backend", return_value=FakeBackend()), \
         patch.object(
             SearchEngine,
             "_embed",
             side_effect=lambda texts: [np.full(384, 0.5, dtype=np.float32) for _ in texts],
         ):
        result = CliRunner().invoke(main, ["--config", str(cfg), "index"])
    assert result.exit_code == 0, result.output
    assert ("OCR: 1 files read, 0 skipped" in result.output) is ocr


def test_index_warns_when_enabled_without_backend(tmp_path: Path) -> None:
    cfg, _ = _config(tmp_path, ocr=True)
    with patch("sahara.storage.state_db.DB_PATH", tmp_path / "state.db"), \
         patch("sahara.ocr.get_backend", return_value=None):
        result = CliRunner().invoke(main, ["--config", str(cfg), "index"])
    assert result.exit_code == 0, result.output
    assert "no OCR backend is available" in result.output
    assert "tesseract" in result.output and "ocr" in result.output


def test_ocr_makes_no_network_call(tmp_path: Path) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network call attempted")

    png = tmp_path / "scan.png"
    png.write_bytes(b"image bytes")
    with patch.object(urllib.request, "urlopen", _refuse), \
         patch.object(socket.socket, "connect", _refuse):
        get_backend("auto")
        TesseractBackend().available()
        extractor = _extractor()
        assert extractor.extract(png) is not None
        assert extractor.extract(_image_pdf(tmp_path / "scan.pdf")) is not None


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not on PATH")
def test_real_tesseract_reads_text(tmp_path: Path) -> None:
    pytest.importorskip("pytesseract")
    image_mod = pytest.importorskip("PIL.Image")
    draw_mod = pytest.importorskip("PIL.ImageDraw")
    font_mod = pytest.importorskip("PIL.ImageFont")
    image = image_mod.new("RGB", (600, 150), "white")
    draw_mod.Draw(image).text((20, 40), "HELLO OCR", fill="black", font=font_mod.load_default(size=64))
    png = tmp_path / "hello.png"
    image.save(png)
    backend = get_backend("tesseract")
    assert backend is not None
    assert "HELLO" in backend.recognize(png.read_bytes()).upper()
