# NOTE: the title/description "longer-wins" overwrite rule (added to fix
# truncated-title clobbering) lives inline inside `enrich_tender_from_analysis`,
# which is DB-session-coupled (queries Tender/ProposalSession, commits, etc.)
# and is not exposed via a pure/testable helper the way commercials are via
# `_apply_commercials_to_tender`. That rule is covered by manual/integration
# verification rather than a unit test here; the numeric fill-if-empty path
# below remains the source of unit coverage for this module.
from types import SimpleNamespace
from datetime import datetime
from app.services.tender_enrichment_service import _apply_commercials_to_tender


def _tender(**kw):
    base = dict(title="ireps #123", description=None, estimated_value=None,
                emd_amount=None, closing_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_fills_empty_numerics_and_date():
    t = _tender()
    commercials = {"advertised_value": "₹ 1,12,55,870.40", "emd": "Rs. 2,25,000/-",
                   "closing_date_raw": "21/07/2026 15:00 hrs", "name_of_work": None}
    changes = _apply_commercials_to_tender(t, commercials)
    assert t.estimated_value == 11255870.40
    assert t.emd_amount == 225000.0
    assert isinstance(t.closing_date, datetime) and t.closing_date.year == 2026
    assert set(changes) >= {"estimated_value", "emd_amount", "closing_date"}


def test_does_not_overwrite_existing_numerics():
    t = _tender(estimated_value=999.0, emd_amount=1.0)
    commercials = {"advertised_value": "₹ 5,00,000", "emd": "Rs. 10,000"}
    _apply_commercials_to_tender(t, commercials)
    assert t.estimated_value == 999.0  # unchanged — fill-if-empty
    assert t.emd_amount == 1.0


def test_unparseable_numeric_is_skipped_not_written():
    t = _tender()
    commercials = {"advertised_value": "as per tender", "emd": "refer NIT"}
    changes = _apply_commercials_to_tender(t, commercials)
    assert t.estimated_value is None
    assert t.emd_amount is None
    assert "estimated_value" not in changes


def test_none_commercials_no_change():
    t = _tender()
    assert _apply_commercials_to_tender(t, None) == {}
    assert _apply_commercials_to_tender(t, {}) == {}
