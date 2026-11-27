"""Optional OCR backends for images and scanned PDFs.

OCR is off by default (``ocr_enabled = false``). When enabled it runs
entirely on this machine: the vision backend uses Apple's on-device Vision
framework (macOS, through pyobjc) and the tesseract backend calls the local
``tesseract`` binary through pytesseract. No backend makes network calls and
Sahara has no cloud OCR; recognised text only goes into the same local search
index as any other extracted text.

Recognised text is never logged. Callers log only counts and file names, at
debug level.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import logging
import shutil
import sys
from typing import Any, Protocol

__all__ = [
    "OCR_BACKEND_NAMES", "OcrBackend", "TesseractBackend", "VisionBackend", "get_backend",
]

logger = logging.getLogger(__name__)

OCR_BACKEND_NAMES = ("auto", "vision", "tesseract")


class OcrBackend(Protocol):
    """A local OCR engine that turns image bytes into text."""

    name: str

    def available(self) -> bool:
        """True when the backend can run here. Never raises."""
        ...

    def recognize(self, image_bytes: bytes) -> str:
        """Return the text recognised in *image_bytes*."""
        ...


class VisionBackend:
    """OCR via Apple's on-device Vision framework (macOS only, pyobjc imported lazily)."""

    name = "vision"

    def available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            importlib.import_module("Vision")
            importlib.import_module("Quartz")
            return True
        except Exception as exc:
            logger.debug("Vision availability check failed: %s", type(exc).__name__)
            return False

    def recognize(self, image_bytes: bytes) -> str:
        vision: Any = importlib.import_module("Vision")
        foundation: Any = importlib.import_module("Foundation")

        data = foundation.NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
        request = vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        handler = vision.VNImageRequestHandler.alloc().initWithData_options_(data, {})
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError("Vision text recognition failed")
        lines: list[tuple[float, float, str]] = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            box = observation.boundingBox()
            # Vision boxes are normalised with the origin at the bottom left.
            top = float(box.origin.y + box.size.height)
            lines.append((-top, float(box.origin.x), str(candidates[0].string())))
        lines.sort()
        return "\n".join(text for _, _, text in lines)


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
    """Backends in ``auto`` preference order."""
    return [VisionBackend(), TesseractBackend()]


def get_backend(name: str) -> OcrBackend | None:
    """Return the named backend if available; ``"auto"`` picks the first available."""
    for backend in _all_backends():
        if name not in ("auto", backend.name):
            continue
        if backend.available():
            return backend
    return None
