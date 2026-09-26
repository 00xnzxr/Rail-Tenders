"""Tests for offline-PDF signature overlay, focusing on the page-size fix.

`_apply_signatures` previously hardcoded the overlay canvas to A4; signatures
on Letter/Legal-sized PDFs (common in offline uploads) landed in the wrong
place. These tests confirm the overlay now sizes to the page's own mediabox
and leaves the page dimensions untouched.
"""

import io

import pytest

reportlab = pytest.importorskip("reportlab")
PyPDF2 = pytest.importorskip("PyPDF2")

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas
from PyPDF2 import PdfReader

from app.services.pdf_generation_service import _apply_signatures
from app.services.storage_service import get_storage_service


def _make_letter_pdf() -> bytes:
    """A single US-Letter (612x792 pt) page PDF."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 720, "Offline document")
    c.showPage()
    c.save()
    return buf.getvalue()


def _make_signature_png() -> bytes:
    """A tiny opaque PNG to use as a signature image."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow required")
    from PIL import Image as PILImage
    img = PILImage.new("RGBA", (200, 80), (10, 30, 90, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _make_big_signature_png() -> bytes:
    """A high-resolution 'scanned' signature PNG (multi-megabyte), like the real
    uploads clients use. Random noise defeats PNG compression so the encoded
    bytes are genuinely large."""
    pytest.importorskip("PIL.Image", reason="Pillow required")
    from PIL import Image as PILImage
    import random
    img = PILImage.new("RGBA", (2400, 1000))
    random.seed(7)
    px = img.load()
    for i in range(2400):
        for j in range(0, 1000, 2):
            px[i, j] = (random.randint(0, 60), random.randint(0, 60), random.randint(0, 90), 255)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _make_multipage_pdf(pages: int) -> bytes:
    """A small text-only multi-page PDF (a few KB per page)."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=letter)
    for p in range(pages):
        c.setFont("Helvetica", 11)
        for line in range(50):
            c.drawString(40, 740 - line * 14, f"Page {p + 1} line {line}: certificate body text")
        c.showPage()
    c.save()
    return buf.getvalue()


class _FakeSignature:
    """Minimal stand-in for a DigitalSignature row — `_apply_signatures` only
    reads the two image-path attributes."""
    def __init__(self, signature_image_path, stamp_image_path=None):
        self.signature_image_path = signature_image_path
        self.stamp_image_path = stamp_image_path


def test_apply_signatures_preserves_letter_page_size(tmp_path):
    storage = get_storage_service()
    sig_key = "tests/offline/sig.png"
    storage.upload_file_sync(sig_key, _make_signature_png(), content_type="image/png")

    src_pdf = _make_letter_pdf()
    src_reader = PdfReader(io.BytesIO(src_pdf))
    src_w = float(src_reader.pages[0].mediabox.width)
    src_h = float(src_reader.pages[0].mediabox.height)

    records = [{
        "signature": _FakeSignature(sig_key),
        "position": "bottom-right",
        "page": "last",
    }]

    out = _apply_signatures(src_pdf, records)

    # Overlay applied: output differs from and is at least as large as input.
    assert out != src_pdf
    assert len(out) >= len(src_pdf)

    # Page size unchanged — the fix must not coerce Letter into A4.
    out_reader = PdfReader(io.BytesIO(out))
    assert len(out_reader.pages) == 1
    assert abs(float(out_reader.pages[0].mediabox.width) - src_w) < 1.0
    assert abs(float(out_reader.pages[0].mediabox.height) - src_h) < 1.0

    storage.delete_file_sync(sig_key)


def test_apply_signatures_bounds_output_size_on_multipage_all():
    """Regression: a 2-3MB source signed with a signature on ALL pages produced
    30MB+ output because the full-resolution image was embedded once per page.
    The signed PDF must stay well under the 8MB cap regardless of page count."""
    storage = get_storage_service()
    sig_key = "tests/offline/sig_big.png"
    storage.upload_file_sync(sig_key, _make_big_signature_png(), content_type="image/png")

    src_pdf = _make_multipage_pdf(30)
    records = [{
        "signature": _FakeSignature(sig_key),
        "position": "custom",
        "position_x": 57.0,
        "position_y": 15.0,
        "page": "all",
    }]

    out = _apply_signatures(src_pdf, records)

    assert len(out) < 8 * 1024 * 1024, (
        f"signed PDF is {len(out) / 1e6:.1f}MB — exceeds the 8MB cap"
    )
    # Overlay actually applied and all pages preserved.
    out_reader = PdfReader(io.BytesIO(out))
    assert len(out_reader.pages) == 30

    storage.delete_file_sync(sig_key)


def test_apply_signatures_no_records_keeps_page_intact():
    # With no signatures the PDF is re-serialised (read->write) but must keep
    # the same page count and size — no overlay, no resize.
    src_pdf = _make_letter_pdf()
    out = _apply_signatures(src_pdf, [])
    src_reader = PdfReader(io.BytesIO(src_pdf))
    out_reader = PdfReader(io.BytesIO(out))
    assert len(out_reader.pages) == len(src_reader.pages) == 1
    assert abs(
        float(out_reader.pages[0].mediabox.width)
        - float(src_reader.pages[0].mediabox.width)
    ) < 1.0
