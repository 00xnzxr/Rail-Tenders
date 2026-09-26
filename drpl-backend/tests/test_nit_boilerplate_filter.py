from app.services.costing.nit_schedule_parser import _BOILERPLATE_RE


def test_matches_existing_specific_footers():
    for s in ["Page 3 of 12", "Run Date/Time: 01/02/2026",
              "TENDER DOCUMENT", "Tender No: 12345",
              "Closing Date/Time: 10:00", "LOCO-SHOP-NR RLY"]:
        assert _BOILERPLATE_RE.match(s), s


def test_matches_generalized_page_artifacts():
    # Differently-worded page artifacts that the fixed enumeration missed.
    for s in ["Page 4", "4 | Page", "Page | 4", "- 7 -"]:
        assert _BOILERPLATE_RE.match(s), s


def test_does_not_match_real_description_lines():
    for s in ["Supply of traction motor bearings",
              "Page mounting bracket assembly",   # 'Page' as a real word
              "Item 5: brake shoe"]:
        assert not _BOILERPLATE_RE.match(s), s
