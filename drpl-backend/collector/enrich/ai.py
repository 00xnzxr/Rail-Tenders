"""
DRPL Collector - reading the bid document with Claude Haiku 4.5.

WHY THIS EXISTS
---------------
Worth and EMD are empty on every tender card in the live DRPL platform, and
they are empty for a good reason: those numbers are not on any listing page, on
either portal. They are inside the bid PDF.

DRPL can already read that PDF -- `tender_analysis_service` and the Deep
Analyzer do exactly this -- but only after the document has been downloaded,
and `nit_link_fetch_enabled` ships as False. So in practice the numbers never
arrive.

This module closes that gap at the point of collection: one Haiku call per new
tender, over the bid PDF, returning the fields DRPL's `TenderInput` already
has columns for. It is deliberately NOT a second analyzer -- it extracts stated
facts and stops. Judgement (relevance, risk, fit) stays with DRPL's own scoring
agents, which run on ingest and have the whole scope profile in their prompt.

WHY IT RUNS BEFORE THE FIRST POST
---------------------------------
`tender_service._update_existing_tender` never assigns `estimated_value` or
`emd_amount` -- not even under `isDetailExtracted`. Only the insert path sets
them. So a value discovered *after* the tender already exists in DRPL would be
silently dropped. Enrichment therefore happens before a tender is shipped, so
those numbers land on the row that gets created.

MODEL
-----
Claude Haiku 4.5 (`claude-haiku-4-5`), as asked for. Notes that matter:
  * Haiku 4.5 does not accept `output_config.effort` -- it errors. Not sent.
  * Adaptive thinking is not a Haiku 4.5 feature either; no `thinking` param.
  * PDFs go in as native base64 `document` blocks, so scanned tenders work
    without an OCR stage. Haiku 4.5 is a 200K-context model, so the page cap
    is 100 -- oversized documents are skipped rather than truncated.
  * $1 / $5 per MTok. Measured on live GeM bid PDFs (2026-09-10): about 20,600
    input and 530 output tokens each -- a PDF goes in as rendered pages, not
    just its text -- so roughly $0.023 per new tender. A sweep that finds a
    dozen new tenders costs about $0.28; a re-run that finds nothing costs
    nothing, because only new tenders are read.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from collector.config import get_settings

logger = logging.getLogger(__name__)

MODEL_DEFAULT = "claude-haiku-4-5"

#: Extraction, not prose. Generous enough for a long eligibility clause.
MAX_TOKENS = 4000

#: The fields we ask for. Every one maps to a real column on DRPL's Tender via
#: TenderInput -- there is no point extracting something with nowhere to go.
#: All nullable: "not stated in this document" is a normal, correct answer, and
#: a guessed number is far worse than an absent one.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "estimated_value_inr": {
            "type": ["number", "null"],
            "description": (
                "Total estimated / advertised contract value in INR as a plain "
                "number. Convert lakh (x100000) and crore (x10000000). Null if "
                "the document does not state one."
            ),
        },
        "emd_amount_inr": {
            "type": ["number", "null"],
            "description": "Earnest Money Deposit / bid security in INR as a plain number, else null.",
        },
        "performance_guarantee_percent": {
            "type": ["number", "null"],
            "description": "Performance guarantee/security as a percentage, e.g. 5 for 5%. Null if absent.",
        },
        "scope_summary": {
            "type": ["string", "null"],
            "description": "2-4 sentences describing the actual work or goods required.",
        },
        "eligibility_criteria": {
            "type": ["string", "null"],
            "description": (
                "Verbatim-ish summary of qualifying requirements: past experience, "
                "turnover, certifications, registrations. Null if none stated."
            ),
        },
        "technical_specifications": {
            "type": ["string", "null"],
            "description": "Key technical requirements, standards or drawing numbers. Null if absent.",
        },
        "delivery_location": {
            "type": ["string", "null"],
            "description": "Consignee / delivery / site location as stated.",
        },
        "delivery_timeline": {
            "type": ["string", "null"],
            "description": "Delivery period or contract duration as stated, e.g. '24 months'.",
        },
        "buyer_contact_name": {"type": ["string", "null"]},
        "buyer_contact_email": {"type": ["string", "null"]},
        "buyer_contact_phone": {"type": ["string", "null"]},
    },
    "required": [
        "estimated_value_inr",
        "emd_amount_inr",
        "performance_guarantee_percent",
        "scope_summary",
        "eligibility_criteria",
        "technical_specifications",
        "delivery_location",
        "delivery_timeline",
        "buyer_contact_name",
        "buyer_contact_email",
        "buyer_contact_phone",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract stated facts from Indian government tender documents (GeM bid documents for Indian Railways).

Rules:
- Report only what the document states. If a field is not stated, return null. Never estimate, infer or carry a number over from a different field.
- Money is INR. Convert to a plain number: "Rs. 12.5 Lakh" -> 1250000, "₹1.2 Cr" -> 12000000, "45,00,000" -> 4500000.
- EMD and estimated value are different things. Do not use one for the other. A bid with "EMD: Nil" has emd_amount_inr 0, not null.
- Quote eligibility criteria closely rather than paraphrasing them away; they decide whether a bid is even possible.
- Be concise everywhere else."""


@dataclass
class Extraction:
    """What Haiku found, plus what it cost."""

    fields: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = MODEL_DEFAULT

    @property
    def cost_usd(self) -> float:
        """Claude Haiku 4.5 list price: $1 / MTok in, $5 / MTok out."""
        return (self.input_tokens / 1_000_000) * 1.0 + (self.output_tokens / 1_000_000) * 5.0

    def to_tender_fields(self) -> dict[str, Any]:
        """Map to TenderInput's camelCase names, dropping nulls.

        Dropping nulls matters: sending an explicit null would overwrite a
        value another source had already found.
        """
        f = self.fields
        out: dict[str, Any] = {}

        def put(key: str, value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str) and not value.strip():
                return
            out[key] = value

        put("estimatedValue", _as_number(f.get("estimated_value_inr")))
        put("emdAmount", _as_number(f.get("emd_amount_inr")))
        put("performanceGuaranteePercent", _as_number(f.get("performance_guarantee_percent")))
        put("fullDescription", f.get("scope_summary"))
        put("eligibilityCriteria", f.get("eligibility_criteria"))
        put("technicalSpecifications", f.get("technical_specifications"))
        put("deliveryLocation", f.get("delivery_location"))
        put("deliveryTimeline", f.get("delivery_timeline"))
        put("buyerContactName", f.get("buyer_contact_name"))
        put("buyerContactEmail", f.get("buyer_contact_email"))
        put("buyerContactPhone", f.get("buyer_contact_phone"))
        return out


def _as_number(v: Any) -> Optional[float]:
    """A number, or None. Never a string, never NaN."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return n if n >= 0 else None


# -- The client ----------------------------------------------------------

_client = None


def get_client():
    """The async Anthropic client, or None when no key is configured.

    None is a supported state, not an error: without a key the collector still
    sweeps and still ships tenders -- it just ships them with the same empty
    Worth and EMD the current system has.
    """
    global _client
    if _client is not None:
        return _client

    s = get_settings()
    if not s.anthropic_api_key:
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning(
            "anthropic package is not installed -- document enrichment disabled. "
            "pip install anthropic"
        )
        return None

    _client = anthropic.AsyncAnthropic(
        api_key=s.anthropic_api_key,
        # The SDK already retries 429/5xx with backoff; a bid PDF is a big
        # request, so give it room rather than failing a whole sweep on a blip.
        timeout=float(s.enrich_timeout_seconds),
        max_retries=3,
    )
    return _client


def reset_for_tests() -> None:
    global _client
    _client = None


def is_available() -> bool:
    return get_client() is not None


# -- Extraction ----------------------------------------------------------


async def extract_from_pdf(
    pdf_bytes: bytes,
    *,
    tender_id: str = "",
    title: str = "",
    model: Optional[str] = None,
) -> Optional[Extraction]:
    """Read one bid PDF. Returns None when unavailable or on any failure.

    Never raises into the sweep. A tender whose document could not be read is
    still worth shipping -- it just arrives with the listing data alone, which
    is exactly where the current system leaves every tender.
    """
    client = get_client()
    if client is None or not pdf_bytes:
        return None

    s = get_settings()
    model = model or s.ai_model or MODEL_DEFAULT

    context = f"Tender {tender_id}: {title}".strip()
    encoded = base64.standard_b64encode(pdf_bytes).decode("ascii")

    try:
        import anthropic

        response = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": encoded,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"{context}\n\n"
                                "Extract the commercial and eligibility facts stated in this "
                                "bid document. Return null for anything it does not state."
                            ),
                        },
                    ],
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}},
        )
    except anthropic.RateLimitError as e:
        logger.warning("enrich: rate limited on %s: %s", tender_id, e)
        return None
    except anthropic.BadRequestError as e:
        # Usually the 100-page cap for a 200K-context model, or a malformed PDF.
        logger.info("enrich: %s rejected by the API: %s", tender_id, e)
        return None
    except anthropic.APIStatusError as e:
        logger.warning("enrich: API error %s on %s", e.status_code, tender_id)
        return None
    except anthropic.APIConnectionError as e:
        logger.warning("enrich: connection error on %s: %s", tender_id, e)
        return None
    except Exception as e:  # noqa: BLE001 -- never fail a sweep for an extra
        logger.warning("enrich: unexpected failure on %s: %s", tender_id, e)
        return None

    # A refusal returns HTTP 200 with no usable content -- check before reading.
    if getattr(response, "stop_reason", None) == "refusal":
        logger.info("enrich: model declined %s", tender_id)
        return None

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError as e:
        logger.info("enrich: %s returned unparseable JSON: %s", tender_id, e)
        return None
    if not isinstance(parsed, dict):
        return None

    usage = getattr(response, "usage", None)
    return Extraction(
        fields=parsed,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        model=model,
    )
