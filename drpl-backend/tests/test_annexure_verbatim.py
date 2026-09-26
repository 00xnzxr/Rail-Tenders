"""Tests for two-pass annexure extraction fidelity.

Covers the defect that motivated the two-pass split: the extractor was
paraphrasing annexures and substituting its own descriptive placeholders (e.g.
"[Name and Address of the Bidder]") where the printed form shows a dotted or
underscored rule. See config.annexure_two_pass_enabled.
"""

import os

import pytest

from app.services.langchain.graphs.annexure_finder_agent import (
    _placeholder_defects,
    _parse_transcription,
    _slice_pdf_pages,
)


class TestPlaceholderDefects:
    """The guard that decides whether a transcription needs a retry."""

    def test_flags_descriptive_placeholder_substituted_for_a_rule(self):
        # Verbatim from the real defective output that prompted this work.
        bad = (
            "**WHEREAS,** [Name and Address of the Bidder] ____\n"
            "**NOW THEREFORE,** We [Name of Bank] _________ hereby undertake"
        )
        defects = _placeholder_defects(bad)
        assert "[Name and Address of the Bidder]" in defects
        assert "[Name of Bank]" in defects

    @pytest.mark.parametrize(
        "marker",
        [
            "[Insert name of the Bidder]",
            "[Insert Name of the Bank]",
            "[Insert Address]",
            "[Insert required Value of Bid Security]",
            "[Insert name(s) of authorized representatives of the Bank]",
            "[insert date of issue]",
            "[Name in Block letters]",
            "[Designation with Code No.]",
            "[to be filled by the bidder]",
            "[As applicable]",
        ],
    )
    def test_allows_instruction_markers_actually_printed_on_the_page(self, marker):
        """IREPS formats really do print these bracketed instructions — every one
        of these was observed in the live CRW-HRT bank guarantee bond. Flagging
        them would trigger pointless retries on correct transcriptions."""
        assert _placeholder_defects(f"WHEREAS, .......... {marker} (hereinafter)") == []

    def test_allows_the_full_real_bond_transcription(self):
        """Regression: the verified-good live transcription must be defect-free."""
        good = (
            "In consideration of the President of India ... having invited the bid "
            "for…………………through Notice inviting tender (NIT) No…………………We have been "
            "informed that………………….[Insert name of the Bidder] (hereinafter called "
            '"the Bidder") intends to submit its bid.\n\n'
            "WHEREAS, the Bidder is required to furnish Bid Security for the sum of "
            "[Insert required Value of Bid Security], in the form of Bank Guarantee.\n\n"
            "1. KNOW ALL MEN that by these present that I/We the undersigned "
            "[Insert name(s) of authorized representatives of the Bank], being fully "
            "authorized to sign...\n"
        )
        assert _placeholder_defects(good) == []

    def test_allows_fill_markers_in_table_cells(self):
        good = "| 1 | [Fill: Project name] | [Fill: Client] |"
        assert _placeholder_defects(good) == []

    def test_flags_bare_description_with_no_rule(self):
        assert _placeholder_defects("Name of Bidder: [Bidder Name Here]") == [
            "[Bidder Name Here]"
        ]

    def test_plain_rules_are_clean(self):
        assert _placeholder_defects(
            "Name of the Bank: ____________________\nDate: .........."
        ) == []

    def test_empty_input(self):
        assert _placeholder_defects("") == []
        assert _placeholder_defects(None) == []

    def test_caps_reported_defects(self):
        many = "\n".join(f"[Name of Party {i}]" for i in range(30))
        assert len(_placeholder_defects(many)) <= 8


DELIMITED = """IDENTIFIER: Annexure-IV
TITLE: BANK GUARANTEE BOND FORMAT FOR BID SECURITY
ORIENTATION: portrait
CLAUSE_COUNT: 5
REACHED_END: yes
NOTES:
---BEGIN TRANSCRIPTION---
**Annexure-IV**

WHEREAS, the Bidder (hereinafter called "the Bidder") has submitted its bid.
1. KNOW ALL MEN that We the undersigned...
---END TRANSCRIPTION---"""


class TestParseTranscription:
    """The body is delimiter-fenced, not JSON — verbatim tender text contains
    unescaped double quotes ("the Bidder") that broke strict json.loads and
    caused good transcriptions to be thrown away."""

    def test_parses_delimited_format(self):
        out = _parse_transcription(DELIMITED)
        assert out["identifier"] == "Annexure-IV"
        assert out["title"] == "BANK GUARANTEE BOND FORMAT FOR BID SECURITY"
        assert out["orientation"] == "portrait"
        assert out["completeness"]["clause_count"] == 5
        assert out["completeness"]["ends_with_source_end"] is True

    def test_body_keeps_unescaped_quotes_verbatim(self):
        out = _parse_transcription(DELIMITED)
        assert '"the Bidder"' in out["markdown_template"]
        assert "---BEGIN" not in out["markdown_template"]
        assert "---END" not in out["markdown_template"]

    def test_body_with_backslashes_and_quotes_survives(self):
        raw = (
            "IDENTIFIER: A-1\n---BEGIN TRANSCRIPTION---\n"
            'Rate per unit (Rs.) \\ per "MT" — see Note "b" below\n'
            "---END TRANSCRIPTION---"
        )
        out = _parse_transcription(raw)
        assert out["markdown_template"] == 'Rate per unit (Rs.) \\ per "MT" — see Note "b" below'

    def test_strips_code_fences_around_delimited_body(self):
        out = _parse_transcription("```text\n" + DELIMITED + "\n```")
        assert out["identifier"] == "Annexure-IV"

    def test_recognises_not_found_sentinel(self):
        assert _parse_transcription("NOT_FOUND")["error"] == "not_found"

    def test_missing_header_fields_do_not_raise(self):
        out = _parse_transcription(
            "---BEGIN TRANSCRIPTION---\nbody text\n---END TRANSCRIPTION---"
        )
        assert out["markdown_template"] == "body text"
        assert out["orientation"] == "portrait"
        assert out["completeness"]["clause_count"] is None

    def test_legacy_json_object_still_accepted(self):
        out = _parse_transcription('{"identifier": "A-2", "markdown_template": "x"}')
        assert out["identifier"] == "A-2"

    def test_legacy_json_not_found_still_accepted(self):
        assert _parse_transcription('{"error": "not_found"}')["error"] == "not_found"

    def test_raises_on_unparseable_response(self):
        with pytest.raises(ValueError):
            _parse_transcription("no markers and no json here at all")

    def test_raises_with_context_on_malformed_json(self):
        with pytest.raises(ValueError, match="neither delimiter-fenced nor valid"):
            _parse_transcription('{"identifier": "A", "markdown_template": "he said "hi""}')


class TestSlicePdfPages:
    """Pass 2 re-reads only the annexure's own pages; the slice must clamp
    safely rather than raise, since discovered page ranges can be off."""

    @pytest.fixture
    def ten_page_pdf(self, tmp_path):
        from PyPDF2 import PdfWriter

        writer = PdfWriter()
        for _ in range(10):
            writer.add_blank_page(width=595, height=842)
        path = tmp_path / "ten.pdf"
        with open(path, "wb") as fh:
            writer.write(fh)
        return str(path)

    def _page_count(self, path):
        from PyPDF2 import PdfReader

        return len(PdfReader(path).pages)

    @pytest.mark.parametrize(
        "first,last,expected",
        [
            (3, 5, 3),      # ordinary interior range
            (1, 1, 1),      # single page
            (9, 20, 2),     # last page overshoots -> clamped to end
            (0, 2, 2),      # padding pushed first below 1 -> clamped to start
            (1, 10, 10),    # whole document
        ],
    )
    def test_clamps_ranges(self, ten_page_pdf, first, last, expected):
        out = _slice_pdf_pages(ten_page_pdf, first, last)
        assert out is not None
        try:
            assert self._page_count(out) == expected
        finally:
            os.unlink(out)

    def test_returns_none_when_range_is_entirely_past_the_end(self, ten_page_pdf):
        assert _slice_pdf_pages(ten_page_pdf, 11, 12) is None

    def test_returns_none_for_unreadable_source(self, tmp_path):
        junk = tmp_path / "not.pdf"
        junk.write_text("definitely not a pdf")
        assert _slice_pdf_pages(str(junk), 1, 2) is None


class TestLetterheadPrecedence:
    """Per-document opt-out > per-document override > tender default > none.

    Before this, annexure workspaces never set letterhead_template_id and no
    fallback existed, so every combined annexure export rendered bare.
    """

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.core.database import Base
        # Import the models so their tables are registered on Base.metadata
        # before create_all — importing app.core.database alone is not enough.
        import app.models.letterhead  # noqa: F401
        import app.models.workspace  # noqa: F401

        engine = create_engine(f"sqlite:///{tmp_path / 'lh.db'}")
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine)()
        yield session
        session.close()

    @pytest.fixture
    def seeded(self, db):
        from app.models.letterhead import LetterheadTemplate
        from app.models.workspace import WorkspaceConfig

        primary = LetterheadTemplate(
            name="DRPL Corporate", company_name_override="DRPL Pvt Ltd", is_active=True
        )
        alt = LetterheadTemplate(
            name="DRPL Alt", company_name_override="DRPL Alt Ltd", is_active=True
        )
        db.add_all([primary, alt])
        db.flush()
        db.add(WorkspaceConfig(tender_id=1, default_letterhead_id=primary.id))
        db.flush()
        return primary, alt

    def _ws(self, db, cid, **kw):
        from app.models.workspace import DocumentWorkspace

        ws = DocumentWorkspace(checklist_item_id=cid, tender_id=1, **kw)
        db.add(ws)
        db.flush()
        return ws

    def test_inherits_tender_default(self, db, seeded):
        from app.services.annexure_export_service import _resolve_letterhead_text

        assert _resolve_letterhead_text(db, 1, self._ws(db, 1)) == "DRPL Pvt Ltd"

    def test_per_item_override_beats_tender_default(self, db, seeded):
        from app.services.annexure_export_service import _resolve_letterhead_text

        _, alt = seeded
        ws = self._ws(db, 2, letterhead_template_id=alt.id)
        assert _resolve_letterhead_text(db, 1, ws) == "DRPL Alt Ltd"

    def test_per_item_disable_beats_tender_default(self, db, seeded):
        """A bank guarantee bond must not carry the bidder's letterhead."""
        from app.services.annexure_export_service import _resolve_letterhead_text

        ws = self._ws(db, 3, letterhead_disabled=True)
        assert _resolve_letterhead_text(db, 1, ws) == ""

    def test_no_letterhead_when_no_default_configured(self, db, seeded):
        from app.models.workspace import WorkspaceConfig
        from app.services.annexure_export_service import _resolve_letterhead_text

        cfg = db.query(WorkspaceConfig).filter_by(tender_id=1).first()
        cfg.default_letterhead_id = None
        db.flush()
        assert _resolve_letterhead_text(db, 1, self._ws(db, 4)) == ""


class TestTranscriptionPromptContent:
    """The prompt is load-bearing — these rules are what stop the paraphrasing."""

    def test_forbids_descriptive_placeholders_explicitly(self):
        from app.services.langchain.graphs.annexure_finder_agent import (
            ANNEXURE_TRANSCRIPTION_PROMPT as P,
        )
        assert "[Name and Address of the Bidder]" in P  # the exact defect, named
        assert "NEVER write a descriptive placeholder" in P

    def test_forbids_inventing_signature_blocks(self):
        from app.services.langchain.graphs.annexure_finder_agent import (
            ANNEXURE_TRANSCRIPTION_PROMPT as P,
        )
        assert "Do NOT add a\nsignature block" in P or "Do NOT add a signature block" in P

    def test_forbids_filling_in_values(self):
        from app.services.langchain.graphs.annexure_finder_agent import (
            ANNEXURE_TRANSCRIPTION_PROMPT as P,
        )
        assert "Never fill in values" in P

    def test_specifies_delimiter_output_not_json(self):
        from app.services.langchain.graphs.annexure_finder_agent import (
            ANNEXURE_TRANSCRIPTION_PROMPT as P,
        )
        assert "---BEGIN TRANSCRIPTION---" in P
        assert "---END TRANSCRIPTION---" in P
        # Escaping is the failure mode the delimiters exist to avoid.
        assert "do NOT need to escape quotes" in P

    def test_discovery_prompt_does_not_ask_for_bodies(self):
        from app.services.langchain.graphs.annexure_finder_agent import (
            ANNEXURE_DISCOVERY_PROMPT as P,
        )
        assert "markdown_template" not in P
        assert "Do NOT transcribe the body" in P


# ── The retry escalates; it does not ask the same model again ──────────────
#
# Pass 2 has always rejected its own output and retried when the transcription
# came back with descriptive placeholders. Both attempts ran on the same
# model, and the documented reason the split exists in the first place is that
# "verbatim transcription of dense legal text is exactly where Haiku's
# instruction-following degrades" -- so the retry was asking the model that
# just failed to try harder, with a sterner prompt.
#
# Nine successive Liluah reports are what that cost. The retry now escalates
# to `annexure_transcription_escalation_model`, per annexure, only after a
# defect has actually been detected -- so a clean transcription still costs
# exactly one cheap call.

_BAD = """IDENTIFIER: Annexure-VII
TITLE: Bank Guarantee Bond
ORIENTATION: portrait
---BEGIN TRANSCRIPTION---
This guarantee is given by [Name of the Bidder] of [Address of the Bidder].
---END TRANSCRIPTION---"""

_GOOD = """IDENTIFIER: Annexure-VII
TITLE: Bank Guarantee Bond
ORIENTATION: portrait
---BEGIN TRANSCRIPTION---
This guarantee is given by ........................ of ........................
---END TRANSCRIPTION---"""


class TestTranscriptionEscalation:
    @pytest.fixture
    def one_page_pdf(self, tmp_path):
        from PyPDF2 import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        path = tmp_path / "one.pdf"
        with open(path, "wb") as fh:
            writer.write(fh)
        return str(path)

    def _run(self, monkeypatch, pdf, replies, *, escalation="claude-sonnet-5"):
        """Drive `_transcribe_one_annexure` with canned model replies.

        Returns ``(annexure, models_used)``.
        """
        import asyncio

        from app.services.langchain.graphs import annexure_finder_agent as af

        models: list[str] = []
        queue = list(replies)

        async def fake_call(**kwargs):
            models.append(kwargs["model_override"])
            return queue.pop(0)

        monkeypatch.setattr(af, "call_ai_with_documents", fake_call, raising=False)
        monkeypatch.setattr(
            "app.services.ai_service.call_ai_with_documents", fake_call, raising=False
        )

        class FakeSettings:
            annexure_transcription_model = "claude-haiku-4-5"
            annexure_transcription_escalation_model = escalation
            annexure_transcription_page_padding = 0

        monkeypatch.setattr(af, "get_settings", lambda: FakeSettings())

        ann = {"identifier": "Annexure-VII", "title": "Bank Guarantee Bond",
               "page_range": [1, 1]}
        out = asyncio.run(
            af._transcribe_one_annexure(ann, pdf, None, 1, "system prompt")
        )
        return out, models

    def test_a_clean_first_attempt_costs_one_cheap_call(self, monkeypatch, one_page_pdf):
        ann, models = self._run(monkeypatch, one_page_pdf, [_GOOD])
        assert models == ["claude-haiku-4-5"], "the happy path must not escalate"
        assert ann["transcription_status"] == "ok"
        assert ann["transcription_meta"]["escalated"] is False
        assert ann["transcription_meta"]["attempts"] == 1

    def test_a_placeholder_defect_escalates_the_retry(self, monkeypatch, one_page_pdf):
        ann, models = self._run(monkeypatch, one_page_pdf, [_BAD, _GOOD])
        assert models == ["claude-haiku-4-5", "claude-sonnet-5"]
        assert ann["transcription_status"] == "ok"
        assert "[Name of the Bidder]" not in ann["markdown_template"]
        meta = ann["transcription_meta"]
        assert meta["escalated"] is True
        assert meta["model"] == "claude-sonnet-5"
        assert meta["attempts"] == 2

    def test_an_unusable_envelope_also_escalates(self, monkeypatch, one_page_pdf):
        ann, models = self._run(monkeypatch, one_page_pdf, ["not a transcription", _GOOD])
        assert models == ["claude-haiku-4-5", "claude-sonnet-5"]
        assert ann["transcription_status"] == "ok"

    def test_an_empty_body_also_escalates(self, monkeypatch, one_page_pdf):
        empty = _GOOD.replace(
            "This guarantee is given by ........................ of ........................",
            "",
        )
        ann, models = self._run(monkeypatch, one_page_pdf, [empty, _GOOD])
        assert models == ["claude-haiku-4-5", "claude-sonnet-5"]

    def test_an_empty_setting_restores_same_model_retries(self, monkeypatch, one_page_pdf):
        """The escape hatch: a deployment that does not want the escalation
        keeps the old behaviour rather than losing the retry."""
        ann, models = self._run(monkeypatch, one_page_pdf, [_BAD, _GOOD], escalation="")
        assert models == ["claude-haiku-4-5", "claude-haiku-4-5"]
        assert ann["transcription_meta"]["escalated"] is False

    def test_a_defect_that_survives_both_attempts_is_recorded_not_hidden(
        self, monkeypatch, one_page_pdf
    ):
        """The one case a reader must be told about: the form is accepted
        because a partial transcription beats none, but it still contains a
        description of a blank where the page printed a rule."""
        ann, models = self._run(monkeypatch, one_page_pdf, [_BAD, _BAD])
        assert models == ["claude-haiku-4-5", "claude-sonnet-5"]
        assert ann["transcription_status"] == "ok_with_placeholders"
        assert ann["transcription_meta"]["placeholder_defects"]


class TestTranscriptionOutcomeIsReported:
    """`placeholder_defects` was computed and then dropped on the floor: the
    nine Liluah reports each found it by reading the output instead."""

    def test_the_stats_count_escalations_and_surviving_defects(self):
        out = [
            {"transcription_status": "ok",
             "transcription_meta": {"escalated": False}},
            {"transcription_status": "ok",
             "transcription_meta": {"escalated": True}},
            {"transcription_status": "ok_with_placeholders",
             "transcription_meta": {"escalated": True, "placeholder_defects": ["[x]"]}},
            {"transcription_status": "slice_failed"},
        ]
        stats: dict = {}
        # The accounting block of _transcribe_annexures, exercised directly on
        # the shapes it has to add up.
        stats["transcribed"] = sum(
            1 for a in out if a.get("transcription_status", "").startswith("ok"))
        stats["transcription_failed"] = sum(
            1 for a in out if not a.get("transcription_status", "").startswith("ok"))
        stats["placeholder_defects"] = sum(
            1 for a in out if a.get("transcription_status") == "ok_with_placeholders")
        stats["escalated"] = sum(
            1 for a in out if (a.get("transcription_meta") or {}).get("escalated"))
        assert stats == {
            "transcribed": 3, "transcription_failed": 1,
            "placeholder_defects": 1, "escalated": 2,
        }

    def test_the_reply_carries_them(self):
        """Read off the source so the keys cannot be quietly dropped again."""
        import inspect

        from app.services.langchain.graphs import annexure_finder_agent as af

        src = inspect.getsource(af.run_annexure_extraction)
        for key in ("transcribed", "transcription_failed", "escalated",
                    "placeholder_defects"):
            assert f'"{key}": stats.get("{key}"' in src, key
