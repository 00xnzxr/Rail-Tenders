"""The RunPod PaddleOCR-VL reader, and where it sits in the scanned-page cascade."""
import pytest
from PIL import Image, ImageDraw

from app.services import advanced_document_parser as adp
from app.services import runpod_ocr_service as ocr


# --- segmentation -----------------------------------------------------------

def _page_with_rows(height=2000, width=800, rows=(100, 400, 700, 1000, 1300, 1600)):
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    for y in rows:
        d.rectangle((40, y, width - 40, y + 60), fill=0)
    return img


def test_bands_are_cut_at_ink_free_rows_and_never_split_a_row():
    img = _page_with_rows()
    bands = ocr.band_bounds(img)
    assert bands
    height = img.size[1]
    for top, bottom in bands:
        assert bottom - top <= int(height * ocr._BAND_MAX_FRACTION) + 1
        for y in (100, 400, 700, 1000, 1300, 1600):
            # a printed row lies wholly inside one band or wholly outside it
            assert not (top < y < bottom < y + 60) and not (y < top < y + 60)
    assert bands[0][0] < 100 + 60 and bands[-1][1] >= 1660


def test_a_blank_page_has_no_bands():
    assert ocr.band_bounds(Image.new("L", (400, 600), 255)) == []


# --- output handling --------------------------------------------------------

def test_table_markup_becomes_pipe_rows():
    raw = ("<fcel>1<fcel>Channel RH 8 mm<fcel>4<fcel>kg<fcel>28.791<fcel>₹ 38.90<fcel>₹ 4,479.37<nl>"
           "<fcel>TOTAL<lcel><lcel><lcel><lcel><lcel><fcel>₹ 14,409.43<nl>")
    assert ocr.cells_to_text(raw).split("\n") == [
        "| 1 | Channel RH 8 mm | 4 | kg | 28.791 | ₹ 38.90 | ₹ 4,479.37",
        "| TOTAL | | | | | | ₹ 14,409.43",
    ]
    assert ocr.cells_to_text("<fcel>Channel for middle\\ntrough floor<fcel>2<nl>") == "| Channel for middle trough floor | 2"


def test_drift_into_other_scripts_or_location_tokens_is_garbage():
    assert ocr.looks_like_garbage("| 5 | Perforated Plate | 2 | kg | \\(\\我那\\) 42.84 | 32,282.59")
    assert ocr.looks_like_garbage("全<|LOC_42|><|LOC_267|>")
    assert not ocr.looks_like_garbage("| 1 | Angle 6 mm x 3249 x 64*48 | 4 | kg | IS: 2062 | ₹ 536.38 | ₹ 2,145.53")
    assert not ocr.looks_like_garbage("")


def test_the_signature_stamp_is_not_content():
    text = ("| TOTAL | ₹ 14,409.43\n"
            "| Signature Not VerifiedDigitally signed by RAKESH R坎NEWIANSAHDate: 2026.04.13 Reason: IREL baS-交易所 | |")
    assert ocr.strip_signature_stamp(text) == "| TOTAL | ₹ 14,409.43"
    assert not ocr.looks_like_garbage(text)


# --- the cascade ------------------------------------------------------------

def _configured(monkeypatch, on=True):
    monkeypatch.setattr(ocr, "is_configured", lambda db=None: on)


@pytest.mark.parametrize("setting, configured, expected", [
    ("claude_then_runpod", True, ("claude", "runpod")),
    ("runpod_then_claude", True, ("runpod", "claude")),
    ("runpod", True, ("runpod",)),
    ("claude", True, ("claude",)),
    ("runpod", False, ("claude",)),
    ("claude_then_runpod", False, ("claude",)),
    ("nonsense", True, ("claude", "runpod")),
])
def test_provider_order_follows_the_setting_and_the_configured_key(monkeypatch, setting, configured, expected):
    from app.services import settings_service
    monkeypatch.setattr(settings_service, "get_setting_value", lambda db, key, default=None: setting)
    _configured(monkeypatch, configured)
    assert adp.page_ocr_provider_order(None) == expected


def _stub_page(monkeypatch, claude, runpod, cached=None, saved=None):
    from app.services import pdf_vision_service as pvs
    monkeypatch.setattr(pvs, "compute_source_key", lambda path: "src")
    monkeypatch.setattr(pvs, "extract_single_page_pdf_bytes", lambda path, i: b"%PDF")
    monkeypatch.setattr(pvs, "compute_page_sha1", lambda b: "sha")
    monkeypatch.setattr(pvs, "get_cached_vision", lambda db, s, i, h: cached)
    def save(db, **kw):
        if saved is not None:
            saved.update(kw)
    monkeypatch.setattr(pvs, "save_cached_vision", save)
    monkeypatch.setattr(pvs, "extract_page_via_claude_vision_sync",
                        lambda path, i, db=None, model=None: claude() if callable(claude) else claude)
    monkeypatch.setattr(ocr, "extract_page_via_runpod_ocr_sync",
                        lambda path, i, db=None: runpod() if callable(runpod) else runpod)


def test_runpod_reads_the_page_claude_could_not(monkeypatch):
    monkeypatch.setattr(adp, "page_ocr_provider_order", lambda db: ("claude", "runpod"))
    saved = {}
    _stub_page(monkeypatch, claude=None, runpod={"text": "| 1 | Bib Cock | ₹ 1,965.25", "model": "runpod:paddleocr-vl"}, saved=saved)
    out = adp._try_page_vision_cached(None, "x.pdf", 0, source_key_cached=None, model="m")
    assert out == {"text": "| 1 | Bib Cock | ₹ 1,965.25", "source_key": "src", "method": "runpod_ocr"}
    assert saved["method"] == "runpod_ocr" and saved["model"] == "runpod:paddleocr-vl"


def test_claude_is_not_called_past_its_page_cap_but_runpod_still_is(monkeypatch):
    monkeypatch.setattr(adp, "page_ocr_provider_order", lambda db: ("claude", "runpod"))
    calls = []

    def claude():
        calls.append("claude")
        return {"text": "claude text"}

    _stub_page(monkeypatch, claude=claude, runpod={"text": "runpod text"})
    out = adp._try_page_vision_cached(None, "x.pdf", 0, source_key_cached=None, model="m", claude_allowed=False)
    assert out["text"] == "runpod text" and calls == []


def test_runpod_first_falls_back_to_claude_when_a_band_is_garbled(monkeypatch):
    monkeypatch.setattr(adp, "page_ocr_provider_order", lambda db: ("runpod", "claude"))
    _stub_page(monkeypatch, claude={"text": "claude text", "model": "claude-haiku-4-5"}, runpod=None)
    out = adp._try_page_vision_cached(None, "x.pdf", 0, source_key_cached=None, model="m")
    assert out["method"] == "claude_vision" and out["text"] == "claude text"


def test_a_cached_page_is_served_whichever_reader_wrote_it(monkeypatch):
    class Row:
        text = "cached"
        method = "runpod_ocr"
    _stub_page(monkeypatch, claude=lambda: pytest.fail("live call"), runpod=lambda: pytest.fail("live call"), cached=Row())
    out = adp._try_page_vision_cached(None, "x.pdf", 0, source_key_cached="src", model="m")
    assert out == {"text": "cached", "source_key": "src", "method": "runpod_ocr"}


def test_the_reader_is_skipped_when_not_configured(monkeypatch):
    monkeypatch.setattr(ocr, "_api_key", lambda db=None: None)
    monkeypatch.setattr(ocr, "_endpoint_id", lambda db=None: None)
    assert ocr.extract_page_via_runpod_ocr_sync("x.pdf", 0) is None
    assert not ocr.is_configured()


def test_a_garbled_band_rejects_the_whole_page(monkeypatch):
    monkeypatch.setattr(ocr, "band_bounds", lambda image: [(0, 10), (10, 20)])
    monkeypatch.setattr(ocr, "_crop_png_b64", lambda image, top, bottom, pad=6: "")
    replies = iter(["<fcel>1<fcel>Angle<fcel>₹ 536.38<nl>", "全<|LOC_42|>"])
    monkeypatch.setattr(ocr, "_run_job", lambda *a, **k: next(replies))
    assert ocr._extract_image(Image.new("L", (10, 20), 255), "k", "e", timeout_s=1, table=True, label="t") is None
    replies = iter(["<fcel>1<fcel>Angle<fcel>₹ 536.38<nl>", "<fcel>TOTAL<fcel>₹ 536.38<nl>"])
    out = ocr._extract_image(Image.new("L", (10, 20), 255), "k", "e", timeout_s=1, table=True, label="t")
    assert out["text"] == "| 1 | Angle | ₹ 536.38\n| TOTAL | ₹ 536.38" and out["bands"] == 2


def test_tesseract_slot_goes_to_runpod_first(monkeypatch):
    _configured(monkeypatch, True)
    monkeypatch.setattr(ocr, "extract_page_via_runpod_ocr_sync", lambda path, i, db=None: {"text": "ocr text"})
    assert adp._ocr_pdf_page("x.pdf", 0) == "ocr text"
    monkeypatch.setattr(ocr, "extract_image_via_runpod_ocr_sync", lambda path, db=None: {"text": "image text"})
    assert adp.extract_text_from_image("x.png")["pages"][0]["text"] == "image text"
