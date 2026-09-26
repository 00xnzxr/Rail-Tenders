"""
DRPL Backend - Shared PDF rasterisation helper.

Rasterises a PDF's pages into a single tall PNG (pages stacked vertically),
the exact format the frontend signature-placement board expects. Both the
workspace preview-pages.png endpoint and the offline-documents signing flow
render from here so drag coordinates always line up with the final PDF.

Uses PyMuPDF (fitz), which ships its own native rendering engine as a wheel —
no system-level poppler binary is required, so this works identically on the
Railway deploy image and on local dev machines.
"""

import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RasterizedPages:
    """One tall PNG of all pages plus the metadata the board needs to compute
    page boundaries without re-measuring the image client-side."""
    png_bytes: bytes
    page_count: int
    page_width_px: int
    page_height_px: int


def rasterize_pdf_pages(pdf_bytes: bytes, dpi: int = 100) -> RasterizedPages:
    """Render every page of `pdf_bytes` to PNG and stack them vertically.

    100 DPI keeps the PNG small enough to ship over the wire while staying
    readable in the ~300px-wide placement board. Raises ValueError when the
    PDF has no pages and RuntimeError when the rasterisation library is
    unavailable / fails.
    """
    try:
        import fitz  # PyMuPDF
        from PIL import Image
    except Exception as e:  # pragma: no cover - import guard
        raise RuntimeError(f"PDF rasterisation libraries unavailable: {e}")

    # PyMuPDF zoom factor: 72 is the PDF's native points-per-inch.
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    page_images = []
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
            for page in pdf:
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                page_images.append(
                    Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                )
    except Exception as e:
        raise RuntimeError(f"Failed to rasterise PDF: {e}")

    if not page_images:
        raise ValueError("No pages rendered")

    page_width = max(img.width for img in page_images)
    # All pages should be the same size at one DPI, but guard against minor
    # drift by padding narrower pages to the max width.
    page_height = page_images[0].height
    total_height = sum(img.height for img in page_images)

    canvas = Image.new("RGB", (page_width, total_height), "white")
    y_offset = 0
    for img in page_images:
        x_offset = (page_width - img.width) // 2  # centre any narrower page
        canvas.paste(img, (x_offset, y_offset))
        y_offset += img.height

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)

    return RasterizedPages(
        png_bytes=buf.getvalue(),
        page_count=len(page_images),
        page_width_px=page_width,
        page_height_px=page_height,
    )


# Header set every rasterised-pages response should expose, so the board can
# read page count + per-page dimensions from a cross-origin fetch.
RASTER_EXPOSE_HEADERS = (
    "X-PDF-Page-Count, X-Image-Page-Height-Px, X-Image-Page-Width-Px"
)


def raster_response_headers(raster: RasterizedPages) -> dict:
    """Build the standard response headers for a rasterised-pages PNG."""
    return {
        "Cache-Control": "no-store",
        "X-PDF-Page-Count": str(raster.page_count),
        "X-Image-Page-Height-Px": str(raster.page_height_px),
        "X-Image-Page-Width-Px": str(raster.page_width_px),
        "Access-Control-Expose-Headers": RASTER_EXPOSE_HEADERS,
    }
