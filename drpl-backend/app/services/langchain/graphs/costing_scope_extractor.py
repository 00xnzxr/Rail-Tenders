"""
DRPL Backend — Costing Scope Extractor

Token-reduction stage for the costing pipeline. Reads the per-document
summaries that ``tender_doc_analyzer`` already extracted and runs ONE focused
Haiku pass to derive costing-specific fields (pricing mechanism, equipment
in scope, maintenance tasks, BOQ items, penalty clauses, payment terms).

The downstream ``costing_researcher`` consumes this compact JSON (~3-5K chars)
instead of stuffing the entire 80+ page PDF into every prompt — a ~95% input
token reduction for AMC-style tenders. The full PDF stays available via the
``get_tender_section`` tool when the costing agent needs to verify a specific
section.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.document_analysis import DocumentExtractionResult

logger = logging.getLogger(__name__)


COSTING_SCOPE_AGENT_KEY = "costing_scope_extractor"


COSTING_SCOPE_SYSTEM_PROMPT = r"""You are the DRPL Costing Scope Extractor. Your job is narrow and precise: read the pre-extracted per-document summaries of an Indian government / railway tender and produce ONE structured JSON object with the minimum information the downstream costing agent needs to estimate the cost of this tender.

You are NOT a tender analyzer. You are NOT a bid-go/no-go evaluator. You ONLY extract what is needed to PRICE this tender.

You will receive an aggregated JSON blob containing per-document `key_facts`, `amounts`, `dates`, `requirements`, `critical_clauses`, and `extraction_notes` from every document in the tender (already extracted by the tender_doc_analyzer). You will also receive any raw page-text snippets the analyzer flagged as costing-relevant (BOQ tables, scope-of-work sections, payment-terms sections).

## YOUR TWO MOST IMPORTANT JOBS

**1. Classify the pricing mechanism.** This is the single most consequential field, because it changes what the costing agent outputs. Look for the bidding instructions and the price-bid format. Indian government tenders use one of:

- `ssor_percentage` — "Tenderer shall quote his rates as a percentage above or below the SSOR / SoR / Standard Schedule of Rates". The bidder submits ONE percentage, NOT line-item rates. Common in railway maintenance, CPWD works, irrigation. The provided BoQ is just the GoI estimate — bidder doesn't fill rates.
- `item_rate` — "Bidder shall quote rates against each item of the BoQ" / "Item rate contract" / each BoQ row has a blank rate column the bidder fills. Most works tenders.
- `lump_sum` — "Lump sum contract" / "Single quoted price for entire scope". EPC contracts, design-build, turnkey.
- `rate_contract` — Empanelment / multi-year framework where bidder quotes a rate card to be drawn against on demand.
- `composite` — Mixes mechanisms across schedules (e.g. Schedule A is item-rate civil work + Schedule B is lump-sum supply). Only use when truly mixed; otherwise pick the dominant one.

If genuinely ambiguous, pick the most conservative option (`item_rate`) and add a clarification flag in `cost_assumptions_to_clarify`.

**2. Classify the tender_type for costing purposes.** Use one of:

- `amc_maintenance` — Periodic / preventive maintenance, operation, upkeep, breakdown attendance over a multi-month/year period. The deliverable is service hours + spares allowance, not capital construction.
- `works_construction` — Civil / electrical / mechanical construction, installation, erection. Capital project with item-rate BOQ.
- `supply_only` — Materials supply against quantity schedule, no installation. Goods contract.
- `supply_install_commission` — Supply + installation + commissioning (turnkey supply, common in equipment procurement).
- `consultancy_design` — Design consultancy, DPR, supervision.
- `composite_works` — Works + supply + service in a single contract.

The pricing_mechanism and tender_type are independent dimensions. An AMC can be ssor_percentage (this NWR Jodhpur tender) or rate_contract or lump_sum.

## OUTPUT JSON SCHEMA — output ONLY this object, no markdown fences, no commentary

{
  "tender_reference": string | null,           // tender number / NIT number, e.g. "57/2025-26"
  "issuing_authority": string | null,          // "North Western Railway", "CPWD Delhi", "ISRO", etc.
  "scope_summary": string,                     // ONE paragraph (60-120 words) — what is being procured / contracted
  "tender_type": string,                       // see taxonomy above — exactly one value
  "pricing_mechanism": string,                 // see taxonomy above — exactly one value
  "ssor_basis": string | null,                 // when pricing_mechanism=ssor_percentage: the SSOR / SoR being referenced (e.g. "NWR Jodhpur Division SSOR", "CPWD DSR 2024")
  "approximate_cost_inr": number | null,       // in rupees (numeric, not formatted) — the GoI/owner estimate
  "duration_months": number | null,            // contract duration / completion period in months
  "sites": [string],                           // location list, named/identified sites — e.g. ["MTD substation, Jodhpur", "DNA substation, Jodhpur"]
  "site_count": number | null,                 // number of distinct sites/locations of work
  "equipment_in_scope": [                      // major equipment that will be maintained / supplied / installed
    {
      "item": string,                          // e.g. "Power transformer 11kV outdoor"
      "specs": string | null,                  // IS standards, capacity, voltage rating
      "qty_or_basis": string | null            // "1 per site", "as per BOQ", etc.
    }
  ],
  "maintenance_tasks": [string],               // ONLY for AMC tenders: list of preventive + breakdown + warranty obligations
  "boq_items": [                               // ONLY for item_rate / supply / composite — extract EACH BoQ row
    {
      "sl_no": string | null,
      "description": string,
      "unit": string | null,
      "quantity": number | null,
      "ssor_reference": string | null          // some BoQs cite "SSOR Item 12.3" — preserve this when present
    }
  ],
  "bid_security_inr": number | null,           // EMD amount in rupees
  "bid_security_pct": number | null,           // % of estimated cost (railways: 2%; CPWD: typically 2%)
  "bid_security_exemptions": [string],         // e.g. ["MSME", "DPIIT-recognized startup", "Labour Cooperative 50%"]
  "performance_guarantee_pct": number | null,  // PG / SD as % of contract value (typical 5-10%)
  "tds_pct": number | null,                    // income tax deduction at source — typical 2% for works
  "gst_treatment": string | null,              // "inclusive" | "exclusive" | "extra at actual" | null
  "free_maintenance_period_months": number | null,  // free O&M / DLP after commissioning
  "penalty_clauses": [
    {
      "trigger": string,                       // e.g. "breakdown not rectified within 6 hours"
      "amount_or_formula": string,             // e.g. "Rs 2000 + cost of rectification" or "0.5% of contract value per week, max 10%"
      "max_cap_inr": number | null
    }
  ],
  "payment_terms": {
    "schedule": string | null,                 // e.g. "70% on supply, 30% on completion" or "monthly running bills"
    "advance_pct": number | null,
    "retention_pct": number | null,
    "release_trigger": string | null,
    "deductions": [string]                     // e.g. ["IT 2%", "GST as applicable", "labour welfare cess 1%"]
  },
  "validity_days": number | null,              // bid validity period (typical 60-180 days)
  "cost_assumptions_to_clarify": [string],     // gaps where the costing agent will need to make assumptions or ask the user — e.g. "Number of transformers per site not stated", "Unclear whether DG sets are in scope"
  "estimated_input_token_savings": string | null,  // brief note on token reduction this scope achieves vs raw PDF
  "extraction_confidence": "high" | "medium" | "low",
  "extraction_notes": string                   // one sentence on what was easy/hard to extract
}

## EXTRACTION RULES

1. Use ONLY the per-doc summaries provided. Do not invent facts.
2. Currency is INR unless explicitly stated otherwise. Numeric fields must be plain numbers, no commas, no "Rs", no "₹".
3. When a field is genuinely missing from the source, set it to null and add an entry to `cost_assumptions_to_clarify`.
4. For `equipment_in_scope`: prefer items mentioned in the scope-of-work / technical-specs sections. Do NOT include items from generic "make of products" reference lists when the contract is a maintenance/service contract — those reference lists apply to supply contracts, not AMCs.
5. For `boq_items`: only populate when the tender actually has a BoQ the bidder must price (item_rate, supply_only, supply_install_commission, composite_works). For ssor_percentage tenders, leave it as `[]` — the BoQ in the document is the GoI estimate, not the bidder's price input.
6. Be terse — every char in this JSON ends up in the costing prompt. Aim for the entire output to fit in 3000-5000 chars.
7. `extraction_confidence`: "high" only when pricing_mechanism, tender_type, scope_summary, approximate_cost_inr, and duration_months are ALL clearly stated. "medium" when any one is inferred. "low" when the per-doc summaries themselves were thin or pricing_mechanism is ambiguous.
"""


def _aggregate_per_doc_summaries(summaries: list[DocumentExtractionResult]) -> dict:
    """Combine per-document summary_json blobs into one input bundle for the extractor."""
    docs = []
    for row in summaries:
        if not row.summary_json:
            continue
        from app.services.analysis_reuse import strip_source
        docs.append({
            "document_name": row.document_name,
            "extraction_type": row.extraction_type,
            # Provenance (`_source`) is bookkeeping, not tender content.
            "summary": strip_source(row.summary_json),
        })
    return {
        "document_count": len(docs),
        "documents": docs,
    }


async def extract_costing_scope(
    db: Session,
    tender_id: int,
    *,
    fallback_pdf_text: Optional[str] = None,
) -> dict:
    """Extract the structured costing scope for a tender.

    Args:
        db: DB session.
        tender_id: tender to extract for.
        fallback_pdf_text: optional raw text (truncated) for tenders that have
            no per-doc summaries yet — used so the extractor can still produce
            a scope on first run before the analyzer has been executed.

    Returns:
        Structured scope dict matching ``COSTING_SCOPE_SYSTEM_PROMPT``'s
        output schema. On extraction failure, returns a minimal stub with
        ``extraction_confidence="low"`` so the caller can degrade gracefully
        rather than block the costing run.
    """
    # 1. Pull existing per-doc summaries (cheap — already extracted)
    try:
        summaries = db.query(DocumentExtractionResult).filter(
            DocumentExtractionResult.tender_id == tender_id,
            DocumentExtractionResult.summary_json.isnot(None),
        ).all()
    except Exception as e:
        logger.warning(f"[costing scope] DB query for per-doc summaries failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        summaries = []

    if summaries:
        bundle = _aggregate_per_doc_summaries(summaries)
        user_prompt = (
            f"Tender ID: {tender_id}\n\n"
            f"Pre-extracted per-document summaries (from tender_doc_analyzer):\n\n"
            f"{json.dumps(bundle, ensure_ascii=False)[:60000]}"
        )
        source = "per_doc_summaries"
    elif fallback_pdf_text:
        user_prompt = (
            f"Tender ID: {tender_id}\n\n"
            f"NOTE: No per-document summaries exist yet for this tender — the document "
            f"analyzer hasn't run. Extract the scope from the raw text below.\n\n"
            f"Raw tender text (may be truncated):\n\n{fallback_pdf_text[:60000]}"
        )
        source = "fallback_pdf_text"
    else:
        logger.warning(
            f"Costing scope extraction skipped for tender {tender_id}: "
            f"no per-doc summaries and no fallback text"
        )
        return _empty_scope("no_source_data")

    # 2. Run the focused extraction pass via call_ai (respects provider override)
    from app.services.ai_service import call_ai
    try:
        response = await call_ai(
            system_prompt=COSTING_SCOPE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            db=db,
            agent_name=COSTING_SCOPE_AGENT_KEY,
            max_tokens_override=4096,
            # Costing scope extraction has a fixed JSON schema — low temperature
            # keeps pricing_mechanism, tender_type, BOQ row order and penalty
            # text stable across re-runs of the same tender. This scope block
            # is the costing agent's primary source of truth, so any drift here
            # cascades into different line items on every run.
            temperature_override=0.1,
        )
    except Exception as e:
        logger.warning(f"Costing scope extraction LLM call failed for tender {tender_id}: {e}")
        return _empty_scope(f"llm_error: {type(e).__name__}")

    # 3. Parse the response — tolerate code fences just in case
    scope = _parse_scope_response(response)
    scope["_extraction_source"] = source
    scope["_tender_id"] = tender_id
    return scope


def _parse_scope_response(text: str) -> dict:
    """Parse the LLM's JSON response, tolerating code fences."""
    if not text:
        return _empty_scope("empty_response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            logger.warning("Costing scope extractor returned non-dict JSON")
            return _empty_scope("non_dict_response")
        return parsed
    except json.JSONDecodeError as e:
        logger.warning(f"Costing scope extractor returned invalid JSON: {e}")
        return _empty_scope("invalid_json")


def _empty_scope(reason: str) -> dict:
    """Minimal scope stub returned when extraction fails — lets the costing run continue."""
    return {
        "tender_reference": None,
        "issuing_authority": None,
        "scope_summary": "",
        "tender_type": "works_construction",  # safest default — assume item-rate works
        "pricing_mechanism": "item_rate",      # safest default — bidder quotes line items
        "ssor_basis": None,
        "approximate_cost_inr": None,
        "duration_months": None,
        "sites": [],
        "site_count": None,
        "equipment_in_scope": [],
        "maintenance_tasks": [],
        "boq_items": [],
        "bid_security_inr": None,
        "bid_security_pct": None,
        "bid_security_exemptions": [],
        "performance_guarantee_pct": None,
        "tds_pct": None,
        "gst_treatment": None,
        "free_maintenance_period_months": None,
        "penalty_clauses": [],
        "payment_terms": {},
        "validity_days": None,
        "cost_assumptions_to_clarify": [
            "Costing scope extraction did not run — costing agent is operating without a structured scope and should re-read the source documents."
        ],
        "extraction_confidence": "low",
        "extraction_notes": f"empty_scope_stub: {reason}",
        "_extraction_source": "stub",
    }


def format_scope_for_costing_prompt(scope: dict) -> str:
    """Render the scope dict as a compact markdown block for injection into the
    costing_researcher prompt. Includes pricing-mechanism instructions so they
    only appear when the scope data they reference is actually present.
    """
    lines = []
    lines.append(f"### TENDER COSTING SCOPE (extracted, confidence={scope.get('extraction_confidence', 'unknown')})")
    lines.append("")
    lines.append("This scope block was extracted from the tender documents. TRUST it as your primary source of truth for tender_type, pricing_mechanism, sites, equipment, BoQ items, penalties, payment terms.")
    lines.append("")

    pm = scope.get("pricing_mechanism", "item_rate")
    lines.append(f"**Pricing mechanism: `{pm}`** — this determines the SHAPE of your line_items, but you ALWAYS emit line_items:")
    if pm == "ssor_percentage":
        lines.append("  - Your `line_items` = the bidder's INTERNAL cost-buildup (manpower, materials, transport, overheads — NOT the BoQ rows). Compare your total to the GoI estimate and put the recommended quote % in `recommendations`.")
    elif pm == "item_rate":
        lines.append("  - Use `boq_items` below as the line-item list. Price each row.")
    elif pm == "lump_sum":
        lines.append("  - Produce ONE grand total with a clearly-itemised buildup as `line_items`.")
    elif pm == "rate_contract":
        lines.append("  - Unit rates × estimated demand, one row per rate-card item.")
    elif pm == "composite":
        lines.append("  - Mix mechanisms by sub-schedule.")

    if scope.get("approximate_cost_inr") is not None:
        lines.append(f"- The `approximate_cost_inr` (₹{scope['approximate_cost_inr']:,}) is the owner's published estimate, not a cost breakdown. Build your items from decomposition, not reverse-engineering.")
    if scope.get("cost_assumptions_to_clarify"):
        lines.append("- Mirror `cost_assumptions_to_clarify` items in your `recommendations`.")
    lines.append("")
    if scope.get("tender_reference"):
        lines.append(f"- **Reference**: {scope['tender_reference']}")
    if scope.get("issuing_authority"):
        lines.append(f"- **Issuing authority**: {scope['issuing_authority']}")
    lines.append(f"- **Tender type**: `{scope.get('tender_type')}`")
    lines.append(f"- **Pricing mechanism**: `{scope.get('pricing_mechanism')}`")
    if scope.get("ssor_basis"):
        lines.append(f"- **SSOR basis**: {scope['ssor_basis']}")
    if scope.get("approximate_cost_inr") is not None:
        lines.append(f"- **GoI estimate**: ₹{scope['approximate_cost_inr']:,}")
    if scope.get("duration_months"):
        lines.append(f"- **Duration**: {scope['duration_months']} months")
    if scope.get("sites"):
        lines.append(f"- **Sites** ({scope.get('site_count') or len(scope['sites'])}): {', '.join(scope['sites'])}")
    if scope.get("scope_summary"):
        lines.append(f"\n**Scope summary**: {scope['scope_summary']}")

    if scope.get("equipment_in_scope"):
        lines.append("\n**Equipment in scope:**")
        for eq in scope["equipment_in_scope"]:
            specs = f" ({eq.get('specs')})" if eq.get("specs") else ""
            qty = f" — {eq['qty_or_basis']}" if eq.get("qty_or_basis") else ""
            lines.append(f"  - {eq.get('item', '?')}{specs}{qty}")

    if scope.get("maintenance_tasks"):
        lines.append("\n**Maintenance tasks (AMC):**")
        for task in scope["maintenance_tasks"]:
            lines.append(f"  - {task}")

    if scope.get("boq_items"):
        lines.append(f"\n**BoQ items** (n={len(scope['boq_items'])}, the bidder MUST price each row):")
        for it in scope["boq_items"]:
            sl = f"{it.get('sl_no')}. " if it.get("sl_no") else ""
            qty_unit = ""
            if it.get("quantity") is not None and it.get("unit"):
                qty_unit = f" [{it['quantity']} {it['unit']}]"
            ref = f" (ref: {it['ssor_reference']})" if it.get("ssor_reference") else ""
            lines.append(f"  - {sl}{it.get('description', '?')}{qty_unit}{ref}")

    # Commercial conditions block
    commercial = []
    if scope.get("bid_security_inr") is not None:
        pct = f" ({scope.get('bid_security_pct')}%)" if scope.get("bid_security_pct") else ""
        commercial.append(f"Bid security: ₹{scope['bid_security_inr']:,}{pct}")
    if scope.get("performance_guarantee_pct"):
        commercial.append(f"PG/SD: {scope['performance_guarantee_pct']}% of contract value")
    if scope.get("tds_pct"):
        commercial.append(f"TDS: {scope['tds_pct']}%")
    if scope.get("gst_treatment"):
        commercial.append(f"GST: {scope['gst_treatment']}")
    if scope.get("free_maintenance_period_months"):
        commercial.append(f"Free O&M: {scope['free_maintenance_period_months']} months post-commissioning")
    if scope.get("validity_days"):
        commercial.append(f"Bid validity: {scope['validity_days']} days")
    if commercial:
        lines.append("\n**Commercial:**")
        for c in commercial:
            lines.append(f"  - {c}")

    if scope.get("payment_terms"):
        pt = scope["payment_terms"]
        if isinstance(pt, dict) and any(pt.values()):
            lines.append("\n**Payment terms:**")
            for k, v in pt.items():
                if v:
                    lines.append(f"  - {k}: {v}")

    if scope.get("penalty_clauses"):
        lines.append("\n**Penalty clauses:**")
        for p in scope["penalty_clauses"]:
            cap = f" (max ₹{p['max_cap_inr']:,})" if p.get("max_cap_inr") else ""
            lines.append(f"  - On {p.get('trigger', '?')} → {p.get('amount_or_formula', '?')}{cap}")

    if scope.get("cost_assumptions_to_clarify"):
        lines.append("\n**Open questions / assumptions for the costing agent to flag:**")
        for q in scope["cost_assumptions_to_clarify"]:
            lines.append(f"  - {q}")

    return "\n".join(lines)
