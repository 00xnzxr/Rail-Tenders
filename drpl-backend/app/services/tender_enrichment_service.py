"""
DRPL Backend — Tender Enrichment Service

After a successful Deep Analyzer run, parse the extracted document
intelligence (per-doc key_facts + synthesis markdown) to populate the
auto-created Tender row's portal, tender_id (real reference), title and
the linked ProposalSession.title.

Only overwrites auto-generated default values — never user-set values.

Idempotent: safe to re-call after every analysis pass.
"""

import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tender_value_parsers import parse_indian_currency, parse_tender_date

logger = logging.getLogger(__name__)


def _apply_commercials_to_tender(tender, commercials: Optional[dict]) -> dict:
    """Fill-if-empty numeric/date fields on a Tender from a per-doc
    `commercials` block. Pure: mutates the passed object, returns a dict of
    changes. Never overwrites a non-empty value; skips unparseable strings.
    """
    changes: dict = {}
    if not isinstance(commercials, dict) or not commercials:
        return changes

    if getattr(tender, "estimated_value", None) in (None, 0, 0.0):
        val = parse_indian_currency(commercials.get("advertised_value"))
        if val is not None:
            tender.estimated_value = val
            changes["estimated_value"] = val
        elif commercials.get("advertised_value"):
            logger.info(
                f"[enrich] advertised_value unparseable, skipped: "
                f"{commercials.get('advertised_value')!r}"
            )

    if getattr(tender, "emd_amount", None) in (None, 0, 0.0):
        emd = parse_indian_currency(commercials.get("emd"))
        if emd is not None:
            tender.emd_amount = emd
            changes["emd_amount"] = emd
        elif commercials.get("emd"):
            logger.info(
                f"[enrich] emd unparseable, skipped: {commercials.get('emd')!r}"
            )

    if getattr(tender, "closing_date", None) is None:
        dt = parse_tender_date(commercials.get("closing_date_raw"))
        if dt is not None:
            tender.closing_date = dt
            changes["closing_date"] = dt.isoformat()
        elif commercials.get("closing_date_raw"):
            logger.info(
                f"[enrich] closing_date unparseable, skipped: "
                f"{commercials.get('closing_date_raw')!r}"
            )
    return changes


# --- Reference patterns ---
# GEM portal references look like "GEM/2025/B/6936354" (also "GeM/...")
GEM_REF_PATTERN = re.compile(r"\b(?:GEM|GeM)/\d{4}/[A-Z]/\d+\b", re.IGNORECASE)
# Indian Railways IREPS / e-tender refs — looser, matches "e-Tender-Elect-G-64-25" etc.
IREPS_REF_PATTERN = re.compile(r"\be-?Tender[-/][A-Za-z0-9\-/]+\b", re.IGNORECASE)
# Generic GEM mention (URL or word) — used as a portal hint when no ref matches
GEM_HINT_PATTERN = re.compile(r"\b(?:gem\.gov\.in|GeM\b|GEM\b)", re.IGNORECASE)


def _is_default_title(title: Optional[str]) -> bool:
    """True if this looks like an auto-generated session/tender title."""
    if not title or not title.strip():
        return True
    t = title.strip().lower()
    return (
        t.startswith("command center")
        or t == "new session"
        or t == "standalone session"
        or t == "tender analysis assistance needed"
        or t.startswith("untitled")
    )


def _detect_portal_and_reference(text: str) -> tuple[str, Optional[str]]:
    """Return (portal, reference_or_None) inferred from analysis text.

    Priority:
      1. GEM/YYYY/L/N pattern → ("GEM", matched_ref)
      2. e-Tender-* pattern   → ("IREPS", matched_ref)
      3. "gem.gov.in" hint    → ("GEM", None)
      4. Default              → ("IREPS", None)
    """
    if not text:
        return "IREPS", None

    m = GEM_REF_PATTERN.search(text)
    if m:
        return "GEM", m.group(0).upper().replace("GEM/", "GEM/")

    m = IREPS_REF_PATTERN.search(text)
    if m:
        return "IREPS", m.group(0)

    if GEM_HINT_PATTERN.search(text):
        return "GEM", None

    return "IREPS", None


def _collect_per_doc_key_facts(db: Session, tender_id: int) -> list[dict]:
    """Pull per-doc summary_json blobs for this tender."""
    from app.models.document_analysis import DocumentExtractionResult

    rows = (
        db.query(DocumentExtractionResult)
        .filter(
            DocumentExtractionResult.tender_id == tender_id,
            DocumentExtractionResult.extraction_type == "per_doc_summary",
        )
        .all()
    )
    return [r.summary_json for r in rows if isinstance(r.summary_json, dict)]


def _pick_first(*values: Optional[str]) -> Optional[str]:
    """Return the first truthy, non-whitespace string from values."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _shorten(text: str, n: int = 80) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _score_facts_richness(facts: dict) -> int:
    """Score a per-doc key_facts blob by how 'primary-document'-like it is.

    Higher = more likely to be the master RFP / NIT (which carries the
    tender reference and the canonical scope description). Annexures,
    addenda, and supplementary docs typically score low.

    Signals (cumulative):
      +8  has a non-empty `tender_reference`  (dominates: a ref-only doc always wins)
      +3  has a non-empty `scope_summary`
      +1  scope_summary is reasonably long (>= 80 chars)
      +1  has a non-empty `issuing_authority`
      +2  has a non-empty `estimated_value`
    """
    if not isinstance(facts, dict):
        return 0
    score = 0
    if (facts.get("tender_reference") or "").strip():
        score += 8
    scope = (facts.get("scope_summary") or "").strip()
    if scope:
        score += 3
        if len(scope) >= 80:
            score += 1
    if (facts.get("issuing_authority") or "").strip():
        score += 1
    if (facts.get("estimated_value") or "").strip():
        score += 2
    return score


def _pick_primary_facts(per_doc_blobs: list[dict]) -> tuple[dict, str]:
    """Pick the most-authoritative per-doc key_facts blob.

    Returns ``(primary_facts, doc_name)``. ``doc_name`` is empty when the
    picker couldn't identify a source doc (e.g. the blob has no doc_name).
    Falls back to the first blob with any non-empty key_facts when all
    scores tie at 0 — preserving legacy behavior so we don't regress on
    single-doc tenders.

    For multi-doc tenders this picks the blob whose key_facts looks most
    like a master RFP (rich set of fields, long scope_summary, has a
    real tender_reference). That's the master NIT bundle most of the time.
    """
    best_score = -1
    best_facts: dict = {}
    best_doc_name: str = ""

    for blob in per_doc_blobs:
        if not isinstance(blob, dict):
            continue
        kf = blob.get("key_facts")
        if not isinstance(kf, dict) or not any(kf.values()):
            continue
        score = _score_facts_richness(kf)
        if score > best_score:
            best_score = score
            best_facts = kf
            best_doc_name = blob.get("doc_name") or ""

    # Legacy fallback: if NO blob scored above 0 (e.g. all key_facts have
    # only obscure fields), use the first non-empty blob.
    if best_score <= 0:
        for blob in per_doc_blobs:
            if not isinstance(blob, dict):
                continue
            kf = blob.get("key_facts")
            if isinstance(kf, dict) and any(kf.values()):
                return kf, blob.get("doc_name") or ""

    return best_facts, best_doc_name


def _pick_primary_blob(db, tender_id: int) -> tuple[dict, Optional[str]]:
    """Return the most-authoritative FULL per-doc summary_json blob (which
    carries both `key_facts` and `commercials`) for this tender.

    Preference order: (1) a blob whose source doc is NIT-typed
    (TenderDocument.document_type == 'nit'); among those, the richest key_facts.
    (2) else the richest key_facts across all blobs. Returns ({}, None) when
    there are no per-doc summaries.
    """
    from app.models.document_analysis import DocumentExtractionResult
    from app.models.tender import TenderDocument

    rows = (
        db.query(DocumentExtractionResult)
        .filter(
            DocumentExtractionResult.tender_id == tender_id,
            DocumentExtractionResult.extraction_type == "per_doc_summary",
        )
        .all()
    )
    blobs = [(r.document_id, r.summary_json) for r in rows
             if isinstance(r.summary_json, dict)]
    if not blobs:
        return {}, None

    nit_doc_ids = {
        d.id for d in db.query(TenderDocument)
        .filter(TenderDocument.tender_id == tender_id,
                TenderDocument.document_type == "nit").all()
    }

    def _rank(item):
        doc_id, blob = item
        kf = blob.get("key_facts") if isinstance(blob.get("key_facts"), dict) else {}
        is_nit = 1 if doc_id in nit_doc_ids else 0
        return (is_nit, _score_facts_richness(kf))

    doc_id, best_blob = max(blobs, key=_rank)
    return best_blob, best_blob.get("doc_name")


def _scan_all_blobs_for(per_doc_blobs: list[dict], field: str) -> Optional[str]:
    """Cross-doc fallback: return the first non-empty value of ``field``
    found in any blob's ``key_facts``. Used when the picked primary doc
    is missing a field (e.g. picker chose a doc with a strong scope but
    the tender_reference lives on the addendum)."""
    for blob in per_doc_blobs:
        if not isinstance(blob, dict):
            continue
        kf = blob.get("key_facts")
        if not isinstance(kf, dict):
            continue
        v = kf.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def enrich_tender_from_analysis(db: Session, tender_id: int) -> dict:
    """Update auto-created Tender + ProposalSession rows with real tender data
    extracted from the Deep Analyzer output.

    Returns a dict describing what was changed (empty dict if nothing changed).
    Never raises — failures are logged and the function returns ``{}``.
    """
    from app.models.tender import Tender
    from app.models.document_analysis import TenderAnalysisSummary
    from app.models.proposal import ProposalSession

    try:
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if not tender:
            logger.info(f"[enrich] tender {tender_id}: row not found — skipping")
            return {}

        # Enrich ALL tenders (was command_center-only). Title/scope overwrite,
        # numerics fill-if-empty — see design 2026-07-21-nit-fetch-and-enrichment.

        summary_row = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .first()
        )
        synthesis_text = (summary_row.requirement_summary or "") if summary_row else ""

        per_doc_facts = _collect_per_doc_key_facts(db, tender_id)
        primary_facts, primary_doc_name = _pick_primary_facts(per_doc_facts)
        logger.info(
            f"[enrich] tender {tender_id}: scanned {len(per_doc_facts)} per-doc "
            f"summaries; picked primary='{primary_doc_name or '(unknown)'}' "
            f"(score={_score_facts_richness(primary_facts)})"
        )

        # 1. Tender reference + portal — try primary doc first, then scan
        # all blobs as cross-doc fallback (the master RFP may not be the
        # first uploaded, and addenda may carry the canonical reference).
        ref_from_facts = _pick_first(primary_facts.get("tender_reference"))
        if not ref_from_facts:
            ref_from_facts = _scan_all_blobs_for(per_doc_facts, "tender_reference")
            if ref_from_facts:
                logger.info(
                    f"[enrich] tender {tender_id}: tender_reference recovered "
                    f"via cross-doc scan: '{ref_from_facts}'"
                )
        # Prefer GEM ref from facts (cleanest source) but verify against text
        portal, ref_from_text = _detect_portal_and_reference(synthesis_text)
        # If facts give a reference and text doesn't, derive portal from facts ref
        tender_reference = ref_from_facts or ref_from_text
        if ref_from_facts and not ref_from_text:
            facts_portal, _ = _detect_portal_and_reference(ref_from_facts)
            portal = facts_portal

        # 2. Name of work (scope_summary preferred) — primary, cross-doc, then markdown grep
        name_of_work = _pick_first(
            primary_facts.get("scope_summary"),
        )
        if not name_of_work:
            name_of_work = _scan_all_blobs_for(per_doc_facts, "scope_summary")
            if name_of_work:
                logger.info(
                    f"[enrich] tender {tender_id}: scope_summary recovered "
                    f"via cross-doc scan ({len(name_of_work)} chars)"
                )
        if not name_of_work:
            # Final fallback: grep the synthesis markdown for "Name of Work" row
            m = re.search(
                r"(?:Name of Work|Scope of Work)[\s\S]{0,200}?[\|:]\s*([^\|\n]{15,200})",
                synthesis_text,
                re.IGNORECASE,
            )
            if m:
                name_of_work = m.group(1).strip().rstrip("|").strip()
                logger.info(
                    f"[enrich] tender {tender_id}: name_of_work recovered "
                    f"via synthesis markdown grep"
                )

        # 3. Issuing authority — primary then cross-doc
        issuing_authority = _pick_first(
            primary_facts.get("issuing_authority"),
        )
        if not issuing_authority:
            issuing_authority = _scan_all_blobs_for(per_doc_facts, "issuing_authority")

        # Nothing usable extracted → leave the row alone
        if not (tender_reference or name_of_work):
            logger.info(
                f"[enrich] tender {tender_id}: no usable reference/name extracted — "
                f"skipping. Scanned {len(per_doc_facts)} per-doc summaries; "
                f"synthesis_text_len={len(synthesis_text)}"
            )
            return {}

        changes: dict = {}

        # Apply portal + tender_id — these are command_center-specific
        # (auto-created) semantics, so guard them now that the outer
        # auto-created guard is gone.
        _auto = tender.portal == "command_center" or (tender.tender_id or "").startswith("cc-")
        if _auto and tender.portal == "command_center":
            tender.portal = portal
            changes["portal"] = portal
        if _auto and (tender.tender_id or "").startswith("cc-") and tender_reference:
            tender.tender_id = tender_reference
            changes["tender_id"] = tender_reference

        # Compose tender + session title
        if tender_reference and name_of_work:
            new_title = f"{tender_reference} — {_shorten(name_of_work, 80)}"
        elif name_of_work:
            new_title = _shorten(name_of_work, 100)
        elif tender_reference:
            new_title = tender_reference
        else:
            new_title = None

        from app.services.tender_service import is_corrupt_title
        # Title: overwrite only when the NIT-derived title is non-corrupt AND
        # strictly longer than the current one (the truncation case we fix) OR
        # the current title is a default/corrupt placeholder. A good full-length
        # scraped title is never shortened/clobbered by an AI mis-read.
        if new_title and not is_corrupt_title(new_title):
            cur = tender.title or ""
            if _is_default_title(cur) or is_corrupt_title(cur) or len(new_title) > len(cur):
                if tender.title != new_title:
                    tender.title = new_title
                    changes["title"] = new_title
        # Scope of work → description: same longer-wins rule (don't replace a rich
        # scraped description with a shorter AI scope summary).
        if name_of_work:
            cur_desc = tender.description or ""
            if not cur_desc or len(name_of_work) > len(cur_desc):
                if tender.description != name_of_work:
                    tender.description = name_of_work
                    changes["description"] = name_of_work

        primary_blob, _blob_doc = _pick_primary_blob(db, tender_id)
        commercials = primary_blob.get("commercials") if isinstance(primary_blob, dict) else None
        changes.update(_apply_commercials_to_tender(tender, commercials))

        if issuing_authority and not tender.organisation:
            tender.organisation = _shorten(issuing_authority, 200)
            changes["organisation"] = tender.organisation

        # Update linked ProposalSession titles
        if new_title:
            sessions = (
                db.query(ProposalSession)
                .filter(ProposalSession.tender_id == tender_id)
                .all()
            )
            session_renames = []
            for s in sessions:
                if _is_default_title(s.title):
                    s.title = new_title
                    session_renames.append(s.id)
            if session_renames:
                changes["session_titles_renamed"] = session_renames

        if changes:
            db.commit()
            logger.info(f"[enrich] tender {tender_id}: applied {changes}")
        else:
            logger.info(
                f"[enrich] tender {tender_id}: extracted data but all fields "
                f"already user-set — no changes"
            )
        return changes

    except Exception as e:
        logger.warning(
            f"[enrich] failed for tender {tender_id}: {type(e).__name__}: {e}",
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return {}
