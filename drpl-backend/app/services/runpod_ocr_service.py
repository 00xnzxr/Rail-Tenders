"""
DRPL Backend - Page OCR through the RunPod PaddleOCR-VL endpoint.

A vLLM serverless endpoint hosting `PaddlePaddle/PaddleOCR-VL` (an
OpenAI-style chat completion behind RunPod's `/run` + `/status` job API).
The model reads a *region* of a page -- one table, one paragraph -- not a
whole page: given a full page it drifts into invented rows and stray
scripts a few lines in. It ships behind a layout detector that the vLLM
endpoint does not host, so the page is segmented here instead: rendered
at `_DPI`, cut into horizontal bands no taller than `_BAND_MAX_FRACTION`
of the page at the nearest ink-free row, and each band is read with the
table prompt (cell markup, converted below to the pipe-separated aligned
text the schedule extractor already reads) or the plain OCR prompt.

Every band is validated (`looks_like_garbage`) before the page's text is
assembled; a band the model garbled is the page's failure, and the caller
falls through to the next provider. This never raises.

Sync-only, like `pdf_vision_service` -- the document parser is synchronous.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_DPI = 200
_BAND_MAX_FRACTION = 0.18
_BAND_MIN_FRACTION = 0.03
_INK_ROW_THRESHOLD = 0.002
_MAX_BANDS_IN_FLIGHT = 6
_POLL_INTERVAL_S = 2.0
_TABLE_PROMPT = "Table Recognition:"
_OCR_PROMPT = "OCR:"
_CELL_TOKEN_RE = re.compile(r"<(?:f|l|u|e|x)cel>")
_LOC_TOKEN_RE = re.compile(r"<\|LOC_\d+\|>")
_NON_LATIN_RE = re.compile(r"[Ͱ-ϿЀ-ӿ֐-ۿ฀-๿぀-ヿ㐀-鿿가-힯]")


def is_configured(db=None) -> bool:
    return bool(_api_key(db) and _endpoint_id(db))


def _api_key(db=None) -> Optional[str]:
    if db is not None:
        try:
            from app.models.platform_setting import PlatformSetting
            row = db.query(PlatformSetting).filter(PlatformSetting.key == "runpod_api_key").first()
            if row and row.value:
                return row.value
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    return get_settings().runpod_api_key or None


def _endpoint_id(db=None) -> Optional[str]:
    if db is not None:
        try:
            from app.services.settings_service import get_setting_value
            v = get_setting_value(db, "runpod_ocr_endpoint_id", None)
            if v:
                return str(v).strip()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
    return get_settings().runpod_ocr_endpoint_id or None


# ── Page segmentation ────────────────────────────────────────────────────────

def render_page(pdf_path: str, page_index: int, dpi: int = _DPI):
    """The page as a greyscale PIL image, or None."""
    try:
        import pymupdf
        from PIL import Image
        doc = pymupdf.open(pdf_path)
        if page_index < 0 or page_index >= len(doc):
            return None
        pix = doc[page_index].get_pixmap(dpi=dpi)
        return Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    except Exception as e:
        logger.warning(f"runpod_ocr: render failed p={page_index}: {e}")
        return None


def band_bounds(image, max_fraction: float = _BAND_MAX_FRACTION) -> list[tuple[int, int]]:
    """(top, bottom) pixel rows of the bands to read: each at most
    `max_fraction` of the page, cut at the lowest ink-free row above that
    limit so a table row is never split, and bands with no ink dropped."""
    import numpy as np
    a = np.asarray(image)
    if a.ndim == 3:
        a = a.mean(axis=2)
    ink = (a < 128).mean(axis=1)
    height = a.shape[0]
    max_h = max(1, int(height * max_fraction))
    min_h = max(1, int(height * _BAND_MIN_FRACTION))
    has_ink = ink > _INK_ROW_THRESHOLD
    bands: list[tuple[int, int]] = []
    start = 0
    while start < height:
        end = min(height, start + max_h)
        if end < height:
            for y in range(end, start + min_h, -1):
                if not has_ink[y]:
                    end = y
                    break
        if has_ink[start:end].any():
            bands.append((start, end))
        start = end
    return bands


def _crop_png_b64(image, top: int, bottom: int, pad: int = 6) -> str:
    width, height = image.size
    crop = image.crop((0, max(0, top - pad), width, min(height, bottom + pad)))
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


# ── Output handling ──────────────────────────────────────────────────────────

def cells_to_text(raw: str) -> str:
    """PaddleOCR-VL's table markup (<fcel> cell, <nl> row, <lcel>/<ucel>
    spans) as pipe-separated rows, the shape the schedule extractor reads."""
    text = raw.replace("<nl>", "\n")
    text = _CELL_TOKEN_RE.sub(" | ", text)
    text = _LOC_TOKEN_RE.sub("", text)
    text = text.replace("\\(", "").replace("\\)", "").replace("\\times", "x")
    # A wrapped cell comes back with a literal backslash-n inside it.
    text = text.replace("\\n", " ")
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        line = re.sub(r"^(\| )+", "| ", line)
        if line.strip("| "):
            lines.append(line)
    return "\n".join(lines)


_STAMP_LINE_RE = re.compile(r"signature\s+not\s+verified|digitally\s+signed|reason:\s*ireps", re.I)


def strip_signature_stamp(text: str) -> str:
    """The IREPS digital-signature stamp overprints the page corner and reads
    as noise in any script; it is not content."""
    return "\n".join(line for line in text.split("\n") if not _STAMP_LINE_RE.search(line))


def looks_like_garbage(text: str) -> bool:
    """A band the model drifted on: scripts the page does not use, or the
    location tokens it emits when it has lost the layout."""
    if not text or not text.strip():
        return False
    if _LOC_TOKEN_RE.search(text):
        return True
    text = strip_signature_stamp(text)
    letters = sum(1 for c in text if c.isalpha())
    foreign = len(_NON_LATIN_RE.findall(text))
    return letters > 0 and foreign / max(letters, 1) > 0.02


# ── RunPod job API ───────────────────────────────────────────────────────────

def _run_job(client: httpx.Client, base: str, headers: dict, png_b64: str, prompt: str,
             timeout_s: float) -> Optional[str]:
    body = {"input": {
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": 3000, "temperature": 0,
    }}
    t0 = time.time()
    resp = client.post(f"{base}/run", headers=headers, json=body)
    if resp.status_code != 200:
        logger.warning(f"runpod_ocr: /run HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    job_id = resp.json().get("id")
    if not job_id:
        return None
    while time.time() - t0 < timeout_s:
        status = client.get(f"{base}/status/{job_id}", headers=headers).json()
        state = status.get("status")
        if state == "COMPLETED":
            out = status.get("output")
            try:
                return out[0]["choices"][0]["message"]["content"]
            except (TypeError, KeyError, IndexError):
                logger.warning(f"runpod_ocr: unexpected output shape: {str(out)[:200]}")
                return None
        if state in ("FAILED", "CANCELLED", "TIMED_OUT"):
            logger.warning(f"runpod_ocr: job {job_id} {state}: {str(status.get('error'))[:200]}")
            return None
        time.sleep(_POLL_INTERVAL_S)
    try:
        client.post(f"{base}/cancel/{job_id}", headers=headers)
    except Exception:
        pass
    logger.warning(f"runpod_ocr: job {job_id} did not finish in {timeout_s:.0f}s")
    return None


def extract_page_via_runpod_ocr_sync(
    pdf_path: str,
    page_index: int,
    *,
    db=None,
    timeout_s: float = 300.0,
    table: bool = True,
) -> Optional[dict]:
    """Read one PDF page through the RunPod endpoint. Returns
    {text, model, bands} or None on any failure (logged, never raised)."""
    key, endpoint = _api_key(db), _endpoint_id(db)
    if not key or not endpoint:
        logger.info("runpod_ocr: not configured (RUNPOD_API_KEY / RUNPOD_OCR_ENDPOINT_ID) -- skipped")
        return None
    image = render_page(pdf_path, page_index)
    if image is None:
        return None
    return _extract_image(image, key, endpoint, timeout_s=timeout_s, table=table, label=f"p{page_index + 1}")


def extract_image_via_runpod_ocr_sync(image_path: str, *, db=None, timeout_s: float = 300.0) -> Optional[dict]:
    key, endpoint = _api_key(db), _endpoint_id(db)
    if not key or not endpoint:
        return None
    try:
        from PIL import Image
        image = Image.open(image_path).convert("L")
    except Exception as e:
        logger.warning(f"runpod_ocr: cannot open image {image_path}: {e}")
        return None
    return _extract_image(image, key, endpoint, timeout_s=timeout_s, table=True, label=image_path)


def _extract_image(image, key: str, endpoint: str, *, timeout_s: float, table: bool, label: str) -> Optional[dict]:
    bands = band_bounds(image)
    if not bands:
        return None
    base = f"https://api.runpod.ai/v2/{endpoint}"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    prompt = _TABLE_PROMPT if table else _OCR_PROMPT
    t0 = time.time()
    try:
        with httpx.Client(timeout=60.0) as client:
            def read(band):
                return _run_job(client, base, headers, _crop_png_b64(image, *band), prompt, timeout_s)
            with ThreadPoolExecutor(max_workers=min(_MAX_BANDS_IN_FLIGHT, len(bands))) as pool:
                raws = list(pool.map(read, bands))
    except Exception as e:
        logger.warning(f"runpod_ocr: {label}: {e}")
        return None
    parts = []
    for band, raw in zip(bands, raws):
        if raw is None:
            logger.warning(f"runpod_ocr: {label}: band {band} returned nothing")
            return None
        text = strip_signature_stamp(cells_to_text(raw) if table else raw.strip())
        if looks_like_garbage(text):
            logger.warning(f"runpod_ocr: {label}: band {band} garbled ({text[:80]!r}); page rejected")
            return None
        parts.append(text)
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        return None
    logger.info(f"runpod_ocr: {label}: {len(bands)} band(s), {len(text)} chars in {time.time() - t0:.1f}s")
    return {"text": text, "model": "runpod:paddleocr-vl", "bands": len(bands)}
