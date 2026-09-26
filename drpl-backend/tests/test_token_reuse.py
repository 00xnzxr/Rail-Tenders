"""Token-spend fixes: cached costing prefix, unchanged-document reuse and the
idempotent chat analysis.

The rule under test throughout: a fresh run must produce exactly what it
produced before. These changes only decide whether a run happens and whether
its unchanging prefix is read from the prompt cache.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.models.document_analysis import DocumentExtractionResult, TenderAnalysisSummary
from app.models.pdf_vision_cache import DocumentPageVisionCache
from app.services import analysis_reuse as ar
from app.services.langchain import failover_model as fm
from app.services.langchain.graphs import enhanced_costing_agent as eca


# ── 1. costing prefix is one cacheable block on Anthropic only ──────────────

def test_costing_system_prompt_is_a_cacheable_block_on_anthropic():
    msg = eca._system_message_for_model("claude-haiku-4-5", "PROMPT TEXT")
    assert isinstance(msg, SystemMessage)
    assert msg.content == [{
        "type": "text",
        "text": "PROMPT TEXT",
        "cache_control": {"type": "ephemeral"},
    }]


def test_costing_system_prompt_stays_a_plain_string_elsewhere():
    for model in ("gpt-5.6-luna", "gemini-3.7-flash", "qwen2.5-7b"):
        msg = eca._system_message_for_model(model, "PROMPT TEXT")
        assert msg.content == "PROMPT TEXT", model


def test_failover_strips_cache_markers_for_a_non_anthropic_provider():
    sys_msg = eca._system_message_for_model("claude-haiku-4-5", "PROMPT")
    human = HumanMessage(content="hi")
    out = fm._strip_cache_control([sys_msg, human])
    assert out[0].content == [{"type": "text", "text": "PROMPT"}]
    assert out[1] is human  # untouched


def test_text_extract_is_dropped_only_when_it_duplicates_attached_pdfs():
    text_only = "--- NIT.pdf ---\n[page 1 · text]\nfoo\n\n[page 2 · text]\nbar"
    assert eca._extract_duplicates_blocks(text_only, attached_blocks=1)
    # a scanned page keeps the extract: the block may be unreadable there
    scanned = "--- NIT.pdf ---\n[page 1 · text]\nfoo\n\n[page 2 · claude_vision]\nbar"
    assert not eca._extract_duplicates_blocks(scanned, attached_blocks=1)
    # more documents extracted than attached keeps the extract
    two_docs = text_only + "\n\n--- TD.pdf ---\n[page 1 · text]\nbaz"
    assert not eca._extract_duplicates_blocks(two_docs, attached_blocks=1)
    assert not eca._extract_duplicates_blocks("", attached_blocks=1)


# ── 2. per-document reuse ───────────────────────────────────────────────────

def test_source_tag_matches_only_the_same_file_model_and_prompt():
    tag = ar.source_tag(sha1="abc", model="claude-haiku-4-5", prompt="P1", page_count=3)
    assert ar.tag_matches(tag, sha1="abc", model="claude-haiku-4-5", prompt="P1")
    assert not ar.tag_matches(tag, sha1="abd", model="claude-haiku-4-5", prompt="P1")
    assert not ar.tag_matches(tag, sha1="abc", model="claude-sonnet-5", prompt="P1")
    assert not ar.tag_matches(tag, sha1="abc", model="claude-haiku-4-5", prompt="P2")
    assert not ar.tag_matches(None, sha1="abc", model="claude-haiku-4-5", prompt="P1")


def test_strip_source_leaves_the_synthesis_payload_as_before():
    summary = {"doc_type": "NIT", "requirements": [1], ar.SOURCE_KEY: {"sha1": "x"}}
    assert ar.strip_source(summary) == {"doc_type": "NIT", "requirements": [1]}
    assert ar.strip_source({"doc_type": "NIT"}) == {"doc_type": "NIT"}


def test_cached_per_doc_summary_is_found_for_the_same_bytes_only(db):
    tid, did = 910001, 910002
    db.query(DocumentExtractionResult).filter(DocumentExtractionResult.tender_id == tid).delete()
    db.commit()
    good = {"doc_type": "NIT", ar.SOURCE_KEY: ar.source_tag(sha1="s1", model="m", prompt="p")}
    db.add(DocumentExtractionResult(
        tender_id=tid, document_id=did, extraction_type="per_doc_summary",
        items=[], summary_json=good,
    ))
    # a parse failure with the same provenance is never reused
    db.add(DocumentExtractionResult(
        tender_id=tid, document_id=did, extraction_type="per_doc_summary",
        items=[], summary_json={**good, "raw_response_preview": "junk"},
    ))
    db.commit()
    hit = ar.find_cached_per_doc_summary(db, tid, did, sha1="s1", model="m", prompt="p")
    assert hit is not None and hit["doc_type"] == "NIT"
    assert ar.find_cached_per_doc_summary(db, tid, did, sha1="s2", model="m", prompt="p") is None
    assert ar.find_cached_per_doc_summary(db, tid, did, sha1="s1", model="m2", prompt="p") is None


def test_unreadable_markers_are_never_reused(db):
    tid, did = 910011, 910012
    db.add(DocumentExtractionResult(
        tender_id=tid, document_id=did, extraction_type="per_doc_summary", items=[],
        summary_json={"unreadable": True, ar.SOURCE_KEY: ar.source_tag(sha1="s", model="m", prompt="p")},
    ))
    db.commit()
    assert ar.find_cached_per_doc_summary(db, tid, did, sha1="s", model="m", prompt="p") is None


# ── 3. whole-analysis freshness and the chat wording that forces a re-run ───

def test_analysis_is_not_current_without_a_completed_report(db):
    tid = 910021
    assert ar.analysis_is_current(db, tid) == (False, "no_summary")
    db.add(TenderAnalysisSummary(tender_id=tid, analysis_status="in_progress"))
    db.commit()
    ok, why = ar.analysis_is_current(db, tid)
    assert not ok and why.startswith("status=")


@pytest.mark.parametrize("message", [
    "please re-analyze tender 42",
    "Analyse tender 42 again",
    "run a fresh analysis of tender 42",
    "redo the analysis",
    "reanalyse it from scratch",
])
def test_wording_that_asks_for_a_new_analysis(message):
    assert ar.wants_fresh_analysis(message)


@pytest.mark.parametrize("message", [
    "analyze tender 42",
    "what does the analysis of tender 42 say about EMD?",
    "cost tender 42",
])
def test_plain_requests_reuse_the_stored_analysis(message):
    assert not ar.wants_fresh_analysis(message)


def test_stored_analysis_result_has_the_shape_of_a_fresh_run(db):
    from app.services.langchain.graphs import chat_agent_wrappers as caw
    tid = 910031
    db.add(TenderAnalysisSummary(
        tender_id=tid, analysis_status="completed",
        requirement_summary="# SECTION 1\nreport", documents_analyzed=2,
        per_doc_unreadable_count=1,
    ))
    db.commit()
    res = caw._stored_analysis_result(db, tid)
    assert res["status"] == "completed"
    assert res["agent_key"] == "deep_analyzer"
    assert res["output_type"] == "document_analysis"
    assert res["structured_data"] == {"report_markdown": "# SECTION 1\nreport"}
    assert res["metrics"]["reused"] is True
    assert res["metrics"]["documents_count"] == 3
    assert "re-analyze" in res["output"]
    assert caw._stored_analysis_result(db, 910032) is None


# ── 4. annexure discovery reuse lives outside the extraction-results table ──

def test_annexure_discovery_round_trips_through_the_vision_cache(db):
    tid = 910041
    found = [{"identifier": "Annexure-A", "title": "Bid form", "page_range": [3, 4], "_source_path": "/tmp/x"}]
    ar.store_annexure_discovery(db, tid, sha1="f" * 40, model="claude-haiku-4-5", prompt="P", annexures=found)
    hit = ar.find_cached_annexure_discovery(db, tid, sha1="f" * 40, model="claude-haiku-4-5", prompt="P")
    assert hit == [{"identifier": "Annexure-A", "title": "Bid form", "page_range": [3, 4]}]
    assert ar.find_cached_annexure_discovery(db, tid, sha1="e" * 40, model="claude-haiku-4-5", prompt="P") is None
    assert ar.find_cached_annexure_discovery(db, tid, sha1="f" * 40, model="claude-haiku-4-5", prompt="P2") is None
    # nothing landed in document_extraction_results, so counts of analysed
    # tenders and per-doc summaries are unaffected
    assert db.query(DocumentExtractionResult).filter(DocumentExtractionResult.tender_id == tid).count() == 0
    row = db.query(DocumentPageVisionCache).filter(DocumentPageVisionCache.method == "annexure_discovery").first()
    assert row is not None and json.loads(row.text)[0]["identifier"] == "Annexure-A"


def test_annexure_bodies_exist_requires_every_identifier(db):
    from app.models.checklist import ChecklistItem
    from app.models.workspace import DocumentWorkspace
    tid = 910051
    item = ChecklistItem(tender_id=tid, item_name="Annexure-A — Bid form",
                         source_section="annexure_finder:Annexure-A", agent_key="annexure_finder")
    db.add(item)
    db.flush()
    db.add(DocumentWorkspace(checklist_item_id=item.id, tender_id=tid,
                             draft_content_markdown="# form", review_status="drafting"))
    db.commit()
    assert ar.annexure_bodies_exist(db, tid, ["Annexure-A"])
    assert not ar.annexure_bodies_exist(db, tid, ["Annexure-A", "Annexure-B"])
    assert not ar.annexure_bodies_exist(db, tid, [])


# ── same bytes under another document row, and twice within one run ────────


def test_a_second_copy_of_the_same_file_reuses_the_first_copys_read(db):
    """A tender holding the same NIT twice (portal copy + chat upload, or a
    re-upload under a new name) read it twice in full."""
    tid = 910021
    db.query(DocumentExtractionResult).filter(DocumentExtractionResult.tender_id == tid).delete()
    db.commit()
    tagged = {"doc_type": "NIT", "doc_id": 1, ar.SOURCE_KEY: ar.source_tag(sha1="same", model="m", prompt="p")}
    db.add(DocumentExtractionResult(tender_id=tid, document_id=1,
                                    extraction_type="per_doc_summary", items=[], summary_json=tagged))
    db.commit()
    hit = ar.find_cached_per_doc_summary(db, tid, 2, sha1="same", model="m", prompt="p")
    assert hit is not None and hit["doc_type"] == "NIT"
    # Different bytes on the sibling never answer.
    assert ar.find_cached_per_doc_summary(db, tid, 2, sha1="other", model="m", prompt="p") is None
    # Another tender's rows are not searched.
    assert ar.find_cached_per_doc_summary(db, tid + 1, 2, sha1="same", model="m", prompt="p") is None


def test_the_same_bytes_twice_in_one_run_are_read_once(db):
    tid = 910031
    ar.remember_per_doc_summary(tid, sha1="dup", model="m", prompt="p",
                                summary={"doc_type": "BOQ", "doc_id": 7})
    hit = ar.find_cached_per_doc_summary(db, tid, 8, sha1="dup", model="m", prompt="p")
    assert hit == {"doc_type": "BOQ", "doc_id": 7}
    assert ar.find_cached_per_doc_summary(db, tid, 8, sha1="dup", model="m2", prompt="p") is None


def test_unreadable_or_failed_reads_are_not_remembered(db):
    tid = 910041
    ar.remember_per_doc_summary(tid, sha1="u", model="m", prompt="p", summary={"unreadable": True})
    ar.remember_per_doc_summary(tid, sha1="r", model="m", prompt="p",
                                summary={"raw_response_preview": "junk"})
    assert ar.find_cached_per_doc_summary(db, tid, 1, sha1="u", model="m", prompt="p") is None
    assert ar.find_cached_per_doc_summary(db, tid, 1, sha1="r", model="m", prompt="p") is None
