"""Generate the scanned OCR fixtures in tests/eval/corpus (deterministic, Pillow only).

    python scripts/make_ocr_fixtures.py

Writes scan_lab_results_fictional.png (an image-only lab-results page) and
scan_loan_letter_fictional.pdf (page 1 real text, page 2 an image only).
Every name, number and organisation in them is invented and marked FICTIONAL.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "eval" / "corpus"

LAB_LINES = [
    "FICTIONAL SAMPLE - NOT A REAL PATIENT",
    "Larkspur Valley Diagnostics (fictional lab)",
    "Patient: Orla Fenwick-Tarrant (fictional)",
    "Specimen ID: LVD-58213-Q",
    "Collected: 2031-04-17",
    "Hemoglobin: 13.8 g/dL",
    "Fasting glucose: 92 mg/dL",
    "Ferritin: 64 ng/mL",
    "Vitamin D: 41 ng/mL",
    "Reviewed by: Dr. Ambrose Selwyn-Hatch (fictional)",
]

LOAN_TEXT_LINES = [
    "FICTIONAL SAMPLE - NOT A REAL LOAN",
    "Brackenridge Mutual Lending - a fictional lender",
    "Loan offer letter for Tamsin Oyelaran-Pike, a fictional customer",
    "Loan reference: BML-77420",
    "Dear Tamsin, we are pleased to offer you a home improvement loan.",
    "Your repayment schedule is enclosed on the next page.",
]

LOAN_SCAN_LINES = [
    "FICTIONAL SAMPLE - REPAYMENT SCHEDULE",
    "Brackenridge Mutual Lending (fictional)",
    "Approval code: QX-4417-ZEPHYR",
    "Principal: 18,500.00",
    "Monthly instalment: 1,284.60",
    "Term: 16 months",
    "First instalment due: 2032-02-09",
]


def render(lines: list[str], size: tuple[int, int], font_size: int) -> Image.Image:
    """Black text on a white greyscale page, one line per entry."""
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=font_size)
    y = font_size * 2
    for line in lines:
        draw.text((font_size * 2, y), line, fill=0, font=font)
        y += int(font_size * 1.8)
    return image


def _pdf_string(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("latin-1") + b")"


def _stream(header: bytes, data: bytes) -> bytes:
    return b"<< %s /Length %d >>\nstream\n%s\nendstream" % (header, len(data), data)


def build_mixed_pdf(text_lines: list[str], scan: Image.Image) -> bytes:
    """A two-page PDF: page 1 Helvetica text, page 2 one embedded greyscale image."""
    text_ops = b"BT /F1 12 Tf 72 720 Td 18 TL " + b" ".join(
        _pdf_string(line) + b" Tj T*" for line in text_lines
    ) + b" ET"
    width, height = scan.size
    image_header = (
        b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8 /Filter /FlateDecode" % (width, height)
    )
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream(b"", text_ops),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 7 0 R"
        b" /Resources << /XObject << /Im1 8 0 R >> >> >>",
        _stream(b"", b"q 612 0 0 792 0 0 cm /Im1 Do Q"),
        _stream(image_header, zlib.compress(scan.tobytes(), 9)),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref,
    )
    return bytes(out)


def main() -> None:
    png = CORPUS / "scan_lab_results_fictional.png"
    render(LAB_LINES, (1200, 900), 36).save(png, "PNG")
    pdf = CORPUS / "scan_loan_letter_fictional.pdf"
    pdf.write_bytes(build_mixed_pdf(LOAN_TEXT_LINES, render(LOAN_SCAN_LINES, (1275, 1650), 40)))
    for path in (png, pdf):
        print(f"wrote {path.relative_to(CORPUS.parents[2])} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
