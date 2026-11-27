"""Optional OCR backends for images and scanned PDFs.

OCR is off by default (``ocr_enabled = false``). When enabled it runs
entirely on this machine: the tesseract backend calls the local ``tesseract``
binary through pytesseract. No backend makes network calls and Sahara has no
cloud OCR; recognised text only goes into the same local search index as any
other extracted text.

Recognised text is never logged. Callers log only counts and file names, at
debug level.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import shutil
from typing import Protocol

__all__ = ["OCR_BACKEND_NAMES", "OcrBackend", "TesseractBackend", "get_backend"]

logger = logging.getLogger(__name__)

OCR_BACKEND_NAMES = ("auto", "tesseract")


class OcrBackend(Protocol):
    """A local OCR engine that turns image bytes into text."""

    name: str

    def available(self) -> bool:
        """True when the backend can run here. Never raises."""
        ...

    def recognize(self, image_bytes: bytes) -> str:
        """Return the text recognised in *image_bytes*."""
        ...


class TesseractBackend:
    """OCR via the local tesseract binary (pytesseract + Pillow, imported lazily)."""

    name = "tesseract"

    def available(self) -> bool:
        try:
            if importlib.util.find_spec("pytesseract") is None:
                return False
            if importlib.util.find_spec("PIL") is None:
                return False
            return shutil.which("tesseract") is not None
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("tesseract availability check failed: %s", exc)
            return False

    def recognize(self, image_bytes: bytes) -> str:
        import pytesseract  # type: ignore[import-not-found,import-untyped,unused-ignore]
        from PIL import Image  # type: ignore[import-not-found,import-untyped,unused-ignore]

        with Image.open(io.BytesIO(image_bytes)) as image:
            return str(pytesseract.image_to_string(image))


def _all_backends() -> list[OcrBackend]:
    return [TesseractBackend()]


def get_backend(name: str) -> OcrBackend | None:
    """Return the named backend if available; ``"auto"`` picks the first available."""
    for backend in _all_backends():
        if name not in ("auto", backend.name):
            continue
        if backend.available():
            return backend
    return None
