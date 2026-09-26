"""Costing must read every document that carries priced scope.

Bug #7 (Liluah tender): the bidder uploaded the NIT and three annexures and
asked for the annexures to be costed. Annexure 2 has 30 priced items; the
platform costed 6. Nothing errored — the run "succeeded" and produced a
plausible sheet missing most of the scope, which is the worst shape of
failure because nothing looks wrong.

The loss was not in extraction. It was in **selection**: the documents were
never opened. `_pick_nit_source_docs` classified its way down to one bucket
and dropped everything else, on two independent paths:

  1. The per-doc Haiku pass tags documents from a fixed vocabulary that
     includes "annexure" as its own type (see `document_analysis_agent`).
     The NIT-class set was ("BOQ", "schedule_of_rates", "RFP") — so a
     price-bearing annexure was, by construction, invisible to costing.

  2. The loop `break`s on the FIRST non-empty bucket. A tender holding both
     a "BOQ" and a "schedule_of_rates" document costed only the first.
     Worse, an annexure that happened to be tagged "BOQ" won the bucket and
     evicted the NIT itself.

  3. A document with no classification yet — every chat upload, until the
     analyzer catches up — was excluded the moment ANY other document had
     been classified, because it appears in no bucket at all. Upload four
     files and ask to cost them in the same breath and that is the normal
     case, not the edge case.

The rule these tests pin is the inversion: a PDF is excluded only on
positive evidence that it carries no priced scope (a drawing, a terms &
conditions booklet). Silence is never evidence. On a costing platform,
reading a document that turns out to hold nothing costs a parse; skipping
one that holds thirty items costs the bid.
"""

import itertools

import pytest

from app.models.document_analysis import DocumentExtractionResult
from app.models.tender import TenderDocument
from app.services.boq_parser_service import _pick_nit_source_docs

# The `db` fixture rolls back, but these helpers commit (the code under test
# runs its own queries). A fresh tender per test is what keeps them isolated.
_TENDER_IDS = itertools.count(991_007)


@pytest.fixture
def tid() -> int:
    return next(_TENDER_IDS)


def _doc(db, tender_id: int, name: str) -> TenderDocument:
    d = TenderDocument(
        tender_id=tender_id,
        file_name=name,
        file_path=f"tenders/{tender_id}/{name}",
        mime_type="application/pdf",
        document_type="chat_upload",
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _classify(db, doc: TenderDocument, doc_type: str) -> None:
    db.add(DocumentExtractionResult(
        tender_id=doc.tender_id,
        document_id=doc.id,
        document_name=doc.file_name,
        extraction_type="per_doc",
        summary_json={"doc_type": doc_type},
    ))
    db.commit()


def _names(docs) -> set:
    return {d.file_name for d in docs}


# ── the reported bug ────────────────────────────────────────────────────────


def test_priced_annexures_are_read(db, tid):
    """Issue #7. The NIT is tagged BOQ, the annexures are tagged `annexure`,
    and the annexures hold the items the bidder asked to be costed."""
    nit = _doc(db, tid, "NIT.pdf")
    a1 = _doc(db, tid, "Annexure-1.pdf")
    a2 = _doc(db, tid, "Annexure-2.pdf")
    a3 = _doc(db, tid, "Annexure-3.pdf")
    _classify(db, nit, "BOQ")
    for a in (a1, a2, a3):
        _classify(db, a, "annexure")

    picked = _pick_nit_source_docs(db, tid)

    assert _names(picked) == {
        "NIT.pdf", "Annexure-1.pdf", "Annexure-2.pdf", "Annexure-3.pdf"
    }, "an annexure holding 30 priced items was never opened"


def test_an_unclassified_upload_is_still_read(db, tid):
    """The analyzer runs per document and the user does not wait for it. A
    document the pass has not reached yet must not be treated as rejected."""
    nit = _doc(db, tid, "NIT.pdf")
    _doc(db, tid, "Annexure-2.pdf")  # uploaded seconds ago, not yet analyzed
    _classify(db, nit, "BOQ")

    picked = _pick_nit_source_docs(db, tid)

    assert _names(picked) == {"NIT.pdf", "Annexure-2.pdf"}


def test_every_schedule_bearing_bucket_is_read_not_just_the_first(db, tid):
    """The old loop broke on the first non-empty bucket, so a tender holding
    both a BOQ and a schedule of rates costed only one of them."""
    boq = _doc(db, tid, "BOQ.pdf")
    sor = _doc(db, tid, "ScheduleOfRates.pdf")
    rfp = _doc(db, tid, "RFP.pdf")
    _classify(db, boq, "BOQ")
    _classify(db, sor, "schedule_of_rates")
    _classify(db, rfp, "RFP")

    picked = _pick_nit_source_docs(db, tid)

    assert _names(picked) == {"BOQ.pdf", "ScheduleOfRates.pdf", "RFP.pdf"}


def test_an_annexure_tagged_boq_does_not_evict_the_nit(db, tid):
    """First-bucket-wins meant the winning bucket replaced the document set
    rather than joining it — a mis-tagged annexure could drop the NIT."""
    nit = _doc(db, tid, "NIT.pdf")
    anx = _doc(db, tid, "Annexure-2.pdf")
    _classify(db, nit, "RFP")
    _classify(db, anx, "BOQ")

    picked = _pick_nit_source_docs(db, tid)

    assert "NIT.pdf" in _names(picked), "the NIT was evicted by an annexure"
    assert "Annexure-2.pdf" in _names(picked)


# ── what stays excluded ─────────────────────────────────────────────────────


def test_drawings_and_terms_are_still_skipped(db, tid):
    """The filter exists for a reason: parsing a drawing set costs vision
    calls and yields nothing. Positive evidence of no scope still excludes."""
    nit = _doc(db, tid, "NIT.pdf")
    dwg = _doc(db, tid, "Drawings.pdf")
    tnc = _doc(db, tid, "TermsAndConditions.pdf")
    _classify(db, nit, "BOQ")
    _classify(db, dwg, "drawing")
    _classify(db, tnc, "terms_conditions")

    picked = _pick_nit_source_docs(db, tid)

    assert _names(picked) == {"NIT.pdf"}


def test_a_tender_of_only_drawings_falls_back_rather_than_costing_nothing(db, tid):
    """Excluding every document would hand costing an empty schedule and no
    signal. A classification that leaves nothing is more likely wrong than
    the tender is empty, so fall back to reading everything."""
    dwg = _doc(db, tid, "Drawings.pdf")
    _classify(db, dwg, "drawing")

    picked = _pick_nit_source_docs(db, tid)

    assert _names(picked) == {"Drawings.pdf"}


# ── preserved behaviour ─────────────────────────────────────────────────────


def test_no_classifications_at_all_reads_everything(db, tid):
    """Pre-v2 tenders have no per-doc pass. Unchanged."""
    _doc(db, tid, "NIT.pdf")
    _doc(db, tid, "Annexure-1.pdf")

    assert _names(_pick_nit_source_docs(db, tid)) == {
        "NIT.pdf", "Annexure-1.pdf"
    }


def test_no_pdfs_is_empty(db, tid):
    assert _pick_nit_source_docs(db, tid) == []


def test_only_this_tenders_documents_are_returned(db, tid):
    mine = _doc(db, tid, "NIT.pdf")
    _classify(db, mine, "BOQ")
    other = _doc(db, tid + 500_000, "OtherTenderNIT.pdf")
    _classify(db, other, "BOQ")

    assert _names(_pick_nit_source_docs(db, tid)) == {"NIT.pdf"}


def test_document_order_is_stable(db, tid):
    """Cross-document dedup is first-doc-wins, so the order documents are
    parsed in decides which copy of a duplicated row survives. It must not
    depend on how the classifier happened to bucket them."""
    a = _doc(db, tid, "A.pdf")
    b = _doc(db, tid, "B.pdf")
    c = _doc(db, tid, "C.pdf")
    _classify(db, b, "BOQ")
    _classify(db, c, "annexure")
    _classify(db, a, "schedule_of_rates")

    picked = _pick_nit_source_docs(db, tid)

    assert [d.id for d in picked] == sorted(d.id for d in (a, b, c))


# ── a document can carry more than one classification ───────────────────────
#
# Nothing deletes a document's previous DocumentExtractionResult rows, so a
# re-analysis adds another one beside the old. Reading the tag with a plain
# dict assignment let an arbitrary last-one-wins decide, which meant a
# document could be skipped because a stale or disagreeing pass had called it
# a drawing. The same exclude-only-on-positive-evidence rule applies to a
# pass disagreeing with itself: one tag saying "schedule" is enough to read
# the file.


def test_a_document_two_passes_disagree_about_is_read(db, tid):
    nit = _doc(db, tid, "NIT.pdf")
    mixed = _doc(db, tid, "Annexure-2.pdf")
    _classify(db, nit, "BOQ")
    _classify(db, mixed, "drawing")   # an earlier pass got it wrong
    _classify(db, mixed, "BOQ")       # a later pass got it right

    assert "Annexure-2.pdf" in _names(_pick_nit_source_docs(db, tid))


def test_the_order_of_the_disagreement_does_not_matter(db, tid):
    """The rows come back in whatever order the query yields; the outcome
    must not depend on which one happens to be last."""
    nit = _doc(db, tid, "NIT.pdf")
    mixed = _doc(db, tid, "Annexure-2.pdf")
    _classify(db, nit, "BOQ")
    _classify(db, mixed, "BOQ")       # right first
    _classify(db, mixed, "drawing")   # wrong second

    assert "Annexure-2.pdf" in _names(_pick_nit_source_docs(db, tid))


def test_a_document_every_pass_calls_non_schedule_is_still_skipped(db, tid):
    """Agreement is still evidence. Two passes both calling it a drawing is
    the case the filter exists for."""
    nit = _doc(db, tid, "NIT.pdf")
    dwg = _doc(db, tid, "Drawings.pdf")
    _classify(db, nit, "BOQ")
    _classify(db, dwg, "drawing")
    _classify(db, dwg, "terms_conditions")

    assert _names(_pick_nit_source_docs(db, tid)) == {"NIT.pdf"}


# ── the same defect in the costing agent's native-PDF path ──────────────────


def test_the_ordinary_pdf_attach_path_does_not_filter_by_doc_type():
    """The costing agent also reads the tender's PDFs directly, as native
    document blocks. On the ordinary path it must pass no allowlist at all —
    an annexure is attached like any other document. Only the budget rebuild
    below narrows it, and only once the schedule has already been captured."""
    import inspect

    from app.services.langchain.graphs import enhanced_costing_agent

    source = inspect.getsource(enhanced_costing_agent.run_costing_react_node)
    first_call = source.index("_build_tender_pdf_content_blocks(")
    window = source[first_call:first_call + 300]
    assert "doc_type_allowlist" not in window, (
        "the ordinary attach path acquired a doc_type filter — annexures "
        "would stop reaching the agent"
    )


def test_the_budget_rebuild_uses_the_trim_policys_own_set():
    """The rebuild runs because `apply_trim_policy` just shed annexed PDFs.
    If it rebuilt with a wider allowlist it would re-attach what the policy
    dropped and the trim would be a no-op — so it must use the policy's own
    set, imported, not a restated copy that can drift from it."""
    import inspect

    from app.services.langchain.graphs import enhanced_costing_agent

    source = inspect.getsource(enhanced_costing_agent)
    assert "doc_type_allowlist=_NIT_CLASS_DOC_TYPES," in source
    assert "_SCHEDULE_BEARING_DOC_TYPES" not in source, (
        "a second copy of the membership rule is how issue #7 happened"
    )


def test_the_document_cap_counts_eligible_documents():
    """`.limit(max_docs)` ran in SQL BEFORE the allowlist filter, so a tender
    whose first three PDFs by id were a drawing set and two T&C booklets
    handed the agent zero documents while the priced schedule sat fourth."""
    import inspect

    from app.services.langchain.graphs.enhanced_costing_agent import (
        _build_tender_pdf_content_blocks,
    )

    source = inspect.getsource(_build_tender_pdf_content_blocks)
    filter_at = source.index("doc_type_by_id.get(d.id, \"\") in doc_type_allowlist")
    cap_at = source.index("docs = docs[:max_docs]")
    assert filter_at < cap_at, (
        "the cap must be applied to the filtered list, not ahead of it"
    )
    assert ".limit(max_docs)" not in source


def test_an_unclassified_document_survives_the_allowlist():
    """Same silence-is-not-evidence rule as the parser: a document the
    per-doc pass has not reached yet has no entry in the doc_type map, and
    `.get(d.id)` returning None must not read as 'excluded'."""
    import inspect

    from app.services.langchain.graphs.enhanced_costing_agent import (
        _build_tender_pdf_content_blocks,
    )

    source = inspect.getsource(_build_tender_pdf_content_blocks)
    assert 'doc_type_by_id.get(d.id, "")' in source

    from app.services.langchain.context_budget import _NIT_CLASS_DOC_TYPES

    # The budget rebuild is deliberately narrow, so "" is NOT in its set —
    # that path runs only when a schedule is already captured. This test
    # pins the lookup shape, which is what makes the default meaningful
    # wherever a caller does pass a permissive allowlist.
    assert "" not in _NIT_CLASS_DOC_TYPES
