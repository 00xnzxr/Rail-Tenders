"""Output-quality diagnosis.

Each check corresponds to a failure this codebase has actually produced and
documented in `config.py`, so the tests build those exact shapes rather than
synthetic ones.
"""

import pytest

from app.models.checklist import ChecklistItem
from app.models.tender import Tender
from app.models.workspace import DocumentWorkspace
from app.services.langchain.graphs.quality_tools import (
    _check_placeholders,
    _render,
    diagnose_tender_outputs,
)


@pytest.fixture
def tender(db):
    import uuid as _uuid

    # portal is NOT NULL and (portal, tender_id) is unique, so give each test
    # its own id rather than sharing one across the session's SQLite file.
    t = Tender(
        portal="ireps",
        tender_id=f"QT-{_uuid.uuid4().hex[:8]}",
        title="Quality Test Tender",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    yield t
    db.query(DocumentWorkspace).filter(DocumentWorkspace.tender_id == t.id).delete()
    db.query(ChecklistItem).filter(ChecklistItem.tender_id == t.id).delete()
    db.query(Tender).filter(Tender.id == t.id).delete()
    db.commit()


def _add_doc(db, tender_id, name, content):
    """A workspace document and the checklist item that carries its name."""
    item = ChecklistItem(tender_id=tender_id, item_name=name)
    db.add(item)
    db.commit()
    db.refresh(item)

    d = DocumentWorkspace(
        tender_id=tender_id,
        checklist_item_id=item.id,
        draft_content_markdown=content,
    )
    db.add(d)
    db.commit()
    return d


# ── the placeholder fingerprint ─────────────────────────────────────────────


def test_detects_described_blanks():
    """A verbatim transcription carries the blank; a paraphrase describes it."""
    assert _check_placeholders("Signature of [Name of Bidder] hereby...")
    assert _check_placeholders("Dated this [Date] day of")
    assert _check_placeholders("We, [Insert full company name], declare")
    assert _check_placeholders("Reference no. XXXXXX")


def test_does_not_flag_legitimate_bracketed_text():
    """Real tender text uses brackets constantly — clause references, notes.
    Flagging those would make the check noise."""
    assert not _check_placeholders("As per clause [4.2] of the agreement")
    assert not _check_placeholders("The bidder shall submit Form A [see Annexure II]")
    assert not _check_placeholders("Signature: ________________________")


# ── diagnosis ───────────────────────────────────────────────────────────────


def test_missing_tender_is_reported_cleanly(db):
    report = diagnose_tender_outputs(db, 99999999)
    assert not report["ok"]
    assert report["problems"][0]["kind"] == "missing_tender"


def test_clean_tender_reports_ok(db, tender):
    _add_doc(db, tender.id, "Annexure I", "A" * 500)
    report = diagnose_tender_outputs(db, tender.id)

    kinds = {p["kind"] for p in report["problems"]}
    assert "paraphrased_annexure" not in kinds
    assert "empty_document" not in kinds


def test_flags_a_paraphrased_annexure(db, tender):
    _add_doc(db, tender.id, "Annexure IV — Bank Guarantee",
             "We, [Name of Bidder], hereby undertake " + "x" * 200)

    report = diagnose_tender_outputs(db, tender.id)

    problem = next(p for p in report["problems"] if p["kind"] == "paraphrased_annexure")
    assert "Annexure IV" in problem["detail"]
    assert problem["fix"] == "regenerate_annexures"
    assert not report["ok"]


def test_flags_an_empty_document(db, tender):
    _add_doc(db, tender.id, "Annexure VII", "")

    report = diagnose_tender_outputs(db, tender.id)

    problem = next(p for p in report["problems"] if p["kind"] == "empty_document")
    assert "Annexure VII" in problem["detail"]


def test_diagnosis_never_mutates(db, tender):
    """It is a read-only tool; if it wrote anything it would need gating."""
    doc = _add_doc(db, tender.id, "Annexure I", "We, [Name of Bidder], ...")
    before = doc.draft_content_markdown

    diagnose_tender_outputs(db, tender.id)

    db.expire_all()
    after = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.id == doc.id
    ).first().draft_content_markdown
    assert after == before


# ── plain-language rendering ────────────────────────────────────────────────


def test_rendering_names_no_tools(db):
    """The user reads this. It must not mention tool names or internals."""
    report = {
        "tender_id": 5,
        "ok": False,
        "checked": ["annexure wording"],
        "problems": [
            {
                "kind": "paraphrased_annexure",
                "detail": "Annexure IV was summarised rather than copied.",
                "fix": "regenerate_annexures",
            }
        ],
    }

    out = _render(report)

    assert "regenerate_annexures" not in out
    assert "paraphrased_annexure" not in out
    assert "Annexure IV" in out
    assert "confirm" in out.lower()


def test_rendering_says_so_when_all_is_well():
    out = _render({"tender_id": 5, "ok": True, "checked": ["annexure wording"], "problems": []})
    assert "looks fine" in out
