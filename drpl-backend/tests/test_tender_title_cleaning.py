from app.services.tender_service import clean_tender_title, is_corrupt_title


def test_clean_strips_tender_type_suffix_and_ellipsis():
    raw = "(i) Coach alteration in electrical system in 1000 Coaches of......\tTender Type:\tOpen"
    assert clean_tender_title(raw) == "(i) Coach alteration in electrical system in 1000 Coaches of"


def test_clean_normalizes_nbsp():
    assert clean_tender_title("Quantity:\xa05370") == "Quantity: 5370"


def test_clean_handles_none_and_empty():
    assert clean_tender_title(None) == ""
    assert clean_tender_title("   ") == ""


def test_clean_leaves_good_title_untouched():
    good = "Annual Maintenance Contract of 4 Nos. 8-wheeler tower wagons"
    assert clean_tender_title(good) == good


def test_is_corrupt_detects_each_marker():
    assert is_corrupt_title("foo......")
    assert is_corrupt_title("foo Tender Type: Open")
    assert is_corrupt_title("Quantity: 5370")
    assert is_corrupt_title("Quantity:\xa05370")
    assert is_corrupt_title("bad\xa0title")


def test_is_corrupt_false_for_clean_title():
    assert not is_corrupt_title("Annual Maintenance Contract of 4 Nos.")
    assert not is_corrupt_title("")
    assert not is_corrupt_title(None)


def test_mid_string_ellipsis_is_not_corrupt():
    # Only TRAILING ellipsis marks corruption; a legit mid-string "..." must be
    # kept (not dropped to a placeholder on insert).
    assert not is_corrupt_title("Supply of ... bolts")
    assert clean_tender_title("Supply of ... bolts") == "Supply of ... bolts"


from types import SimpleNamespace
from app.services.tender_service import _update_existing_tender


def _existing(title):
    return SimpleNamespace(
        title=title, status="open", closing_date=None, document_links=[],
        is_detail_extracted=False, description="", updated_at=None,
    )


def _incoming(title):
    # Minimal TenderInput-like stub with only the fields _update_existing_tender reads.
    return SimpleNamespace(
        title=title, status=None, closingDate=None, documentLinks=None,
        isDetailExtracted=False, description=None,
    )


def test_update_overwrites_corrupt_stored_title_with_clean():
    existing = _existing("Coach alteration ...of......\tTender Type:\tOpen")
    _update_existing_tender(existing, _incoming("Coach alteration in electrical system full title"))
    assert existing.title == "Coach alteration in electrical system full title"


def test_update_does_not_replace_good_with_shorter():
    existing = _existing("A complete and correct long tender title here")
    _update_existing_tender(existing, _incoming("Short title"))
    assert existing.title == "A complete and correct long tender title here"


def test_update_replaces_good_with_strictly_longer_clean():
    existing = _existing("Short title")
    _update_existing_tender(existing, _incoming("Short title with much more useful detail"))
    assert existing.title == "Short title with much more useful detail"


def test_update_ignores_incoming_corrupt_title():
    existing = _existing("A complete and correct long tender title here")
    _update_existing_tender(existing, _incoming("Quantity:\xa05370"))
    assert existing.title == "A complete and correct long tender title here"
