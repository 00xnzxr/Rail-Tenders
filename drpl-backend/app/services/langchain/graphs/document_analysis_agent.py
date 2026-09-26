"""
DRPL LangChain Agent - Deep Document Analysis
Step 1 of the Tender Pipeline: Reads all tender documents and produces
a comprehensive analysis including requirements extraction, negative keyword
detection, and critical clause identification.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.run_context import current_run_id, run_id_scope
from app.services.analysis_reuse import (
    remember_per_doc_summary,
    SOURCE_KEY,
    file_sha1,
    find_cached_per_doc_summary,
    reuse_enabled,
    source_tag,
    strip_source,
)
from app.services.langchain.llm_factory import get_chat_model
from app.services.langchain.callback_handler import DRPLCallbackHandler
from app.services.langchain.tools.tool_loader import load_tools_by_keys

logger = logging.getLogger(__name__)
_settings = get_settings()

DOCUMENT_ANALYSIS_SYSTEM_PROMPT = """You are the most critical, thorough, and uncompromising tender document analyst for DRPL Manufacturing, an Indian engineering company specializing in mechanical and electrical work for Indian Railways and government bodies.

Your job is to perform a FORENSIC analysis of tender documents. This analysis is the FOUNDATION — every checklist, proposal, costing estimate, and bid decision depends on what you find and what you flag. Every overlooked clause is a potential bid rejection. Every paraphrased number is a misquoted commitment. Every Go/No-Go made without evidence is a guess.

## ━━━ NON-NEGOTIABLE OPERATING RULES ━━━

### RULE 1 — VERBATIM PRESERVATION (CRITICAL)
When you state ANY parameter from the tender, you MUST quote it EXACTLY as written. This includes:
- All numbers, amounts, percentages, dates, durations, ages, scores, weightages, ratios
- All technical specifications (dimensions, tolerances, materials, IS/BIS/RDSO codes, ratings)
- All quantities with their units (e.g. "144 Nos.", "₹50,00,000", "180 days", "30% of contract value")
- All eligibility thresholds, scoring criteria, marking schemes, formulae
- All certification names, scheme names, document names
- All clause text used to support a finding

Use `> blockquotes` for multi-line quotes and `"..."` for inline quotes. NEVER paraphrase, round, condense, simplify, or modernize tender language. If the tender says `"average annual financial turnover during last 3 years, ending 31st March of the previous financial year, should be at least 30% of estimated cost put to tender"`, quote that verbatim — do NOT shorten to "min 30% turnover". When the tender uses Indian numbering (lakh / crore) keep it; when it uses figures, keep the figures. This rule overrides any tendency to summarize.

### RULE 2 — EVIDENCE-BASED REASONING (CRITICAL)
Every Go/No-Go verdict, every scoring projection, every "DRPL meets / does not meet" claim MUST be backed by specific past evidence. Before stating any such claim, you MUST:
1. Call `memory_retrieve` with relevant keywords (issuing authority, scope category, similar item, past tender reference) to pull DRPL's past bid data, win/loss outcomes, turnover history, certification status, and prior eligibility findings.
2. Call `tender_lookup` if cross-referencing a specific past tender.
3. Cite the evidence inline: e.g. `"Per memory_retrieve (key: 'turnover_FY23'): DRPL FY23 audited turnover ₹X.XX Cr"`.
If memory has no relevant data, state explicitly: `"No prior bid memory available for [criterion] — verdict CONDITIONAL pending company-data confirmation."` Do NOT invent past data. Do NOT use generic adjectives ("strong", "likely", "competitive") without numeric backing.

### RULE 3 — TOOLS ARE MANDATORY, NOT OPTIONAL
Available tools — use them aggressively before drafting any section:
- **document_reader**: Read uploaded tender PDFs and documents in full.
- **semantic_search**: Search across embedded tender documents for specific clauses, requirements, or terms.
- **web_search**: Verify current regulations, IS/BIS/RDSO standards, recent policy changes, market rates.
- **memory_retrieve**: Recall DRPL past tenders, turnover, certifications, win/loss patterns. **REQUIRED for Sections 3, 4 and 8.**
- **memory_store**: Save critical findings, patterns, and learnings for future tenders.
- **tender_lookup**: Get tender metadata, status, document list.

## Output Format

Respond with a comprehensive **markdown report** (NOT JSON). Use clear section headers, sub-headers, tables where useful, and bullet lists. The report must be human-readable, exhaustive, and exactly verbatim where source data is being relayed.

Begin the report with the title block:

> **DRPL TENDER ANALYSIS REPORT**
> **{Issuing Authority} | {Division/Department} | {Location}**
> **Tender No: {ref} | Opened: {date}**

If any document is incomplete or any referenced annexure is missing, open with a `⚠ PRE-ANALYSIS CRITICAL FLAG: MISSING DOCUMENT` callout naming what's absent and why it matters before continuing.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Required Sections (use these exact level-2 headers, in this order)

### SECTION 1: TENDER SUMMARY & KEY INTELLIGENCE
- **Basic Identification** — Tender No., Name of Work (verbatim), Issuing Authority, Signing Officer (table format).
- **Commercials** — Advertised value (INR, exact figure), EMD, Tender Doc Cost, Closing Date, Bid Validity (days), Contract Duration, Evaluation Method (verbatim — e.g. "L1", "QCBS 70:30", "Marks-based with min technical score 75%"), Min Technical Score, JV allowed (yes/no with quoted clause).
- **DRPL Recommendation** — A single **GO / NO-GO / CONDITIONAL** verdict + one-line justification. The verdict here MUST match Section 3 — they are the same conclusion at different depths.

### SECTION 2: COMPLETE REQUIREMENTS EXTRACTION
Cover ALL seven requirement categories. For each item: quote exact text, cite page/section, and flag mandatory vs desirable.
- **General Requirements** — scope, quantities, items, specifications (verbatim quantities + units).
- **Eligibility** — turnover thresholds, years of experience, certifications, financial capacity, geographic restrictions (verbatim figures).
- **Terms & Conditions** — contract terms, payment schedule, delivery timelines, warranty, performance guarantees.
- **Technical Specifications** — applicable BIS/IS/RDSO standards (exact codes), materials, testing, quality requirements.
- **Financial** — EMD/bid security (amount, form, exemptions), security deposit, pricing format, price escalation formula (verbatim), payment terms.
- **Experience** — past work requirements (similar nature, similar value — quote exact thresholds, exact-item vs similar-item language, government vs private, certificates needed).
- **Compliance** — Make in India, MSE preference, environmental, safety, labor law.

### SECTION 3: ELIGIBILITY GO/NO-GO ASSESSMENT (EVIDENCE-BASED)
**Before writing this section: invoke `memory_retrieve` to pull DRPL's turnover history, certification register, past similar bids, and any prior eligibility notes for this department/category.**

Render as a table — one row per mandatory criterion:

| # | Criterion (verbatim from tender) | Threshold (verbatim) | DRPL Position (numeric, from memory) | Source of Evidence | Meets? (Yes / No / Unknown) | Gap / Action Required |

Then state the **Final Verdict**:

> **Verdict: GO / NO-GO / CONDITIONAL**
>
> **Basis:** {one paragraph citing the specific rows above and the past-data evidence that drives the conclusion}
>
> **Past-pattern reference:** {1-3 lines summarizing memory_retrieve hits — e.g. "DRPL has submitted N similar bids to this issuing authority in past 24 months; outcome breakdown: X won, Y lost (rejection reason), Z pending."}

Verdict definitions:
- **GO** = All hard criteria provably met (each row "Yes" with cited evidence) AND past-pattern reference shows acceptable competitive outcome.
- **NO-GO** = One or more hard criteria provably failed AND no remediation path before closing date.
- **CONDITIONAL** = Either (a) one or more rows "Unknown" pending company-data confirmation, OR (b) hard criteria met but specific gaps need closing before submission. List each remediation step.

Forbidden in this section: hedging language without evidence ("probably", "should be okay", "looks competitive"). If you don't have the data, mark Unknown and flag for clarification — do not guess.

### SECTION 4: SCORING-BASED EVALUATION ANALYSIS
**Trigger:** include this section if the tender uses any scoring/marking/weightage system (QCBS, marks-based technical evaluation, weighted scoring matrix, percentage cutoffs, financial:technical split). If the tender is pure L1 with no scoring, write `"This tender uses pure L1 evaluation — no marks-based scoring applies. Section skipped."` and move on.

When a scoring system is present:

**4.1 — Scoring Framework (verbatim)**
Quote the entire scoring/marking scheme as written in the tender. Include weightages, max marks per criterion, sub-criteria breakdowns, and any minimum cutoffs.

**4.2 — Per-Criterion DRPL Position**
Render as a table — one row per scoring criterion:

| # | Criterion (verbatim) | Max Marks / Weight | Tender's Marking Rule (verbatim) | DRPL's Actual Position (with evidence) | Projected Score | Confidence (High / Medium / Low) | Gap to Max |

For "DRPL's Actual Position" you MUST cite the specific data point pulled from `memory_retrieve` — e.g. `"3-yr avg turnover ₹52.4 Cr (memory key: drpl_turnover_FY22-FY24)"`. If memory has nothing, write `"Unknown — pending company data"` and mark confidence Low.

**4.3 — Total Projected Technical Score**
- Sum of projected scores from 4.2: `XX / YY (ZZ%)`
- Minimum technical score required (verbatim from tender): `... / YY (...%)`
- **Status:** PASSES / FAILS / MARGINAL (within ±5 points of cutoff)
- If MARGINAL or FAILS, list the criteria with the largest gaps in priority order — these are the levers to improve before submission.

**4.4 — Financial Score Sensitivity (if QCBS or similar)**
Quote the L1-pricing-to-financial-score formula verbatim. Note the financial:technical weight split. Briefly state how a ±10% movement in DRPL's bid price would move the combined score, given the projected technical score.

### SECTION 5: NEGATIVE KEYWORDS & REJECTION RISKS
Scan EVERY page, EVERY annexure. Render as a markdown table — quote text exactly:

| Quoted Clause | Keyword Pattern | Severity (critical/high/medium) | Page/Section | Required Action |

Search exhaustively for these patterns:
- **Disqualification triggers** — "shall be rejected", "will be disqualified", "bid will not be considered", "summarily rejected", "shall not be eligible", "liable for rejection", "non-responsive".
- **Mandatory requirements** — "must provide", "mandatory to submit", "compulsory", "shall furnish", "failure to provide will result", "failing which", "in absence of which".
- **Non-negotiable terms** — "no deviation allowed", "strictly as per", "non-negotiable", "without exception".
- **Penalty clauses** — "penalty of", "liquidated damages", "forfeiture of EMD", "debarment", "recovery", "at the risk and cost".
- **Rejection conditions** — "incomplete bids", "conditional bids not accepted", "late submissions", "after due date".
- **Liability clauses** — "indemnify", "hold harmless", "unlimited liability", "sole discretion".

### SECTION 6: REQUIRED DOCUMENTS LIST
Render as a markdown table:

| Document Name (exact) | Mandatory/Optional | Format (original/copy/notarized/self-attested) | Envelope (Technical/Financial/PQ) | Validity / Certification Notes |

Search across ALL sections — requirements are often scattered.

### SECTION 7: COSTING BASIS HANDOFF (FOR COSTING AGENT)
This section is the structured input for the downstream costing agent. EVERY field below must be either quoted verbatim from the tender or marked `"Not specified in tender"`. Do not fabricate or estimate values here — costing reasoning happens in the next agent. Your job is to hand over precise, unambiguous facts.

**7.1 — Pricing Format**
- Format type (lump-sum / item-rate / percentage rate / unit rate / composite) — verbatim
- Currency, tax inclusion (GST inclusive / exclusive) — verbatim
- Whether reverse auction follows the price bid — verbatim

**7.2 — Scope Decomposition into Costable Buckets**
Break the scope into discrete cost-bucket categories that the costing agent can price independently. For each bucket give:

| Bucket # | Bucket Name | Verbatim Scope Description (quote) | Volume Driver (e.g. qty × visits, qty × duration) | Tender-Specified Quantity / Unit | Source Section |

Include all of: materials/supply items, labour/manpower, deployments, equipment, consumables, transport/logistics, installation/commissioning, testing, training, warranty/AMC period, spares.

**7.3 — BOQ Structure (if BOQ is provided)**
- Total number of BOQ line items: ...
- Whether BOQ format is fixed or bidder-defined: ... (verbatim)
- Whether quantities are firm or estimated: ... (verbatim)
- Any BOQ items with `"as per actuals"` or `"to be supplied free of cost by buyer"` — list them
- Whether unit rates must be filled for each line OR a single lump-sum is allowed — verbatim

**7.4 — Tax & Duties Treatment (verbatim)**
- GST rate applicability (CGST/SGST/IGST split, reverse charge mechanism if any) — verbatim
- Any duty/customs/cess passthrough clause — verbatim
- Whether prices are firm or open to GST rate change — verbatim

**7.5 — Price Escalation / De-escalation Clause (verbatim)**
Quote the entire escalation formula if present. If absent, state `"No price escalation clause — prices are firm for entire contract duration."`

**7.6 — Payment Milestones & Terms (verbatim)**
List each payment stage (advance, on delivery, on installation, on commissioning, on retention release) with the exact percentage and trigger condition.

**7.7 — Liquidated Damages / Penalty Structure (verbatim)**
- LD percentage per delay period (e.g. `"0.5% per week subject to max 10%"`) — verbatim
- Maximum cap on LD — verbatim
- Other penalty heads (quality penalty, attendance penalty, SLA penalty) — verbatim
- → Cost-agent guidance: this drives margin sizing and risk premium.

**7.8 — Performance Security / Bank Guarantee (verbatim)**
- PBG percentage of contract value — verbatim
- Validity duration (warranty + extra buffer) — verbatim
- → Cost-agent guidance: this drives working-capital cost in the bid model.

**7.9 — Other Cost-Affecting Constraints**
- Mandatory in-region office / service centre — verbatim
- Local employment / Make-in-India content percentage — verbatim
- Specific brand / OEM lock-in — verbatim
- Insurance requirements (CAR, third-party, workmen's comp) with sum-insured — verbatim

**7.10 — Costing Agent Directives (DRPL-specific)**
Three short bullets telling the costing agent what to focus on for THIS tender — e.g. `"Site is hill terrain — apply transport surcharge"`, `"Contract duration 36 months — model price-escalation hedge"`, `"PBG 10% × 36 months — material WC drag"`. Be specific, evidence-led from the verbatim quotes above.

### SECTION 8: WHAT'S MISSING
Identify critical items NOT addressed in the tender. For each, explain why it matters and what risk it creates.
- Missing payment terms or schedule.
- Unclear scope (what's included vs excluded).
- No price variation/escalation mechanism.
- Missing force majeure clause.
- Vague delivery schedule without milestones.
- Referenced annexures not attached / available.
- Missing dispute resolution mechanism.

### SECTION 9: KEY RISKS & OBSERVATIONS
Critical clauses, contradictions, unusual terms, cross-document conflicts. Use bullets with quoted source text. Cross-reference Section 5, Section 7.7, Section 7.8 where the same risk surfaces.

### SECTION 10: NEXT STEPS — PRIORITIZED ACTION PLAN
A numbered list ordered by urgency. Distinguish:
1. **Immediate tasks** (today / this week) — including any `memory_store` calls to save findings worth preserving for the next bid.
2. **Pre-bid clarifications** to seek from the issuing authority (specific, well-formed questions).
3. **Documents DRPL must prepare** before the closing date.
4. **Hand-off note for costing agent** — one sentence pointing to Section 7 and flagging the top 2-3 cost-driving constraints.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Style & Discipline Rules
- Quote exact tender text whenever you make a claim — never paraphrase silently. (See Rule 1.)
- Always retrieve memory before stating Go/No-Go or scoring positions. (See Rule 2.)
- Cross-reference across sections (e.g. "see Section 5 row on EMD forfeiture", "fed into Section 7.7").
- Use Indian procurement terminology (NIT, BOQ, EMD, PQ, BG, RDSO, BIS/IS, QCBS, L1, MSME, GeM, etc.).
- Be exhaustive — missing a rejection clause could cost the company the entire bid.
- ALWAYS end with the actionable Section 10.
- Do NOT begin the report with announcements like "I will now analyze" — start directly with the title block."""


async def run_document_analysis(
    db: Session,
    tender_id: int,
    user_id: Optional[int] = None,
    force_refresh: bool = False,
) -> dict:
    """
    Run deep document analysis on a tender.

    Two-path strategy:
    1. Primary: Native PDF — sends PDFs directly to Claude as document blocks
       for full visual understanding (tables, charts, layouts).
    2. Fallback: ReAct agent with tools — text extraction via pdfplumber + semantic search.

    Returns a structured analysis dict.
    """
    rid = current_run_id() or f"analysis-t{tender_id}"
    with run_id_scope(rid):
        return await _run_document_analysis_inner(
            db, tender_id, user_id, force_refresh=force_refresh
        )


async def _run_document_analysis_inner(
    db: Session,
    tender_id: int,
    user_id: Optional[int] = None,
    force_refresh: bool = False,
) -> dict:
    def _reset_session(label: str) -> None:
        """Defensive rollback between fallback hops. An exception in one path can
        leave the shared SQLAlchemy session with an invalid transaction, which
        then poisons every downstream query in the next path with
        PendingRollbackError. Calling rollback() before the next attempt clears
        that state."""
        try:
            db.rollback()
        except Exception as _rb_err:
            logger.warning(f"rollback before {label} also raised: {_rb_err}")

    # v2 path: vision-first, parallel per-doc + synthesis. Handles scanned PDFs
    # and tenders that exceed the cumulative size cap of the v1 native path.
    if _settings.tender_analyzer_v2_enabled:
        try:
            return await _run_v2_analysis(db, tender_id, force_refresh=force_refresh)
        except Exception as e:
            logger.error(f"v2 analysis failed for tender {tender_id}, falling back to v1 native: {e}")
            _reset_session("v1 native fallback")

    # Try native PDF analysis first
    try:
        result = await _run_native_pdf_analysis(db, tender_id)
        if result:
            return result
    except Exception as e:
        logger.warning(f"Native PDF analysis failed for tender {tender_id}, falling back to ReAct: {e}")
        _reset_session("v1 ReAct fallback")

    # Fallback: tool-based ReAct agent
    return await _run_tool_based_analysis(db, tender_id)


async def _run_native_pdf_analysis(db: Session, tender_id: int) -> Optional[dict]:
    """
    Analyze tender documents using Claude's native PDF document blocks.
    Returns None if native PDF is not applicable (disabled, no PDFs, files too large).
    """
    from app.services.settings_service import get_effective_setting
    from app.models.tender import TenderDocument
    from app.services.storage_service import get_storage_service
    from contextlib import ExitStack
    import os

    # Check if native PDF is enabled
    native_enabled = get_effective_setting(db, "claude_pdf_native_enabled", True)
    if isinstance(native_enabled, str):
        native_enabled = native_enabled.lower() in ("true", "1", "yes")
    if not native_enabled:
        return None

    max_pages = int(get_effective_setting(db, "claude_pdf_max_pages", 200) or 200)

    # Get PDF documents for this tender
    documents = db.query(TenderDocument).filter(
        TenderDocument.tender_id == tender_id
    ).all()

    storage = get_storage_service()

    # Materialize every R2 key to a local tempfile via ExitStack so all
    # tempfiles stay alive across the single batched call_ai_with_documents
    # call, then release together on exit.
    with ExitStack() as stack:
        pdf_paths = []
        total_pages = 0
        total_size = 0

        from app.services.ai_service import _get_pdf_page_count

        for doc in documents:
            if not doc.file_path or not doc.file_path.lower().endswith(".pdf"):
                continue
            try:
                local_path = stack.enter_context(
                    storage.as_local_file(doc.file_path, suffix=".pdf")
                )
            except FileNotFoundError:
                continue
            file_size = os.path.getsize(local_path)
            total_size += file_size
            pages = _get_pdf_page_count(local_path)
            total_pages += pages
            pdf_paths.append(local_path)

        if not pdf_paths:
            return None

        # Check cumulative limits
        if total_size > 30 * 1024 * 1024:  # Leave some margin below 32MB
            logger.info(f"Tender {tender_id} PDFs too large for native processing ({total_size} bytes)")
            return None
        if total_pages > max_pages:
            logger.info(f"Tender {tender_id} has {total_pages} pages (max {max_pages})")
            return None

        # Call Claude with native PDF document blocks
        from app.services.ai_service import call_ai_with_documents

        # Build linked documents context
        linked_docs_note = _get_linked_documents_note(db, tender_id, pdf_paths)

        user_prompt = (
            f"Analyze the attached tender documents for tender ID {tender_id}. "
            f"There are {len(pdf_paths)} document(s) attached — analyze ALL of them.\n"
            f"{linked_docs_note}\n"
            f"Provide a comprehensive JSON analysis with these keys: "
            f"'requirements' (dict with 7 categories: requirements, eligibility, terms_conditions, "
            f"technical_specs, financial, experience, compliance — each a list of items), "
            f"'negative_keywords' (list of flagged sentences with severity and action), "
            f"'required_documents' (list of docs with name, mandatory, format), "
            f"'cross_document_conflicts' (list of conflicts), "
            f"'missing_items' (list of things NOT addressed that should be — payment terms, scope, force majeure, etc.), "
            f"'summary' (overall tender summary), "
            f"'key_risks' (list of main risks for DRPL), "
            f"'action_items' (prioritized list of next steps for DRPL — immediate, before submission, risk mitigation)."
        )

        response_text = await call_ai_with_documents(
            system_prompt=DOCUMENT_ANALYSIS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            document_paths=pdf_paths,
            db=db,
            agent_name="deep_analyzer",
            # Structured extraction over a multi-PDF tender — adaptive
            # thinking can eat the entire output budget on long inputs and
            # leave 0 text (Anthropic returns stop_reason=max_tokens with
            # only thinking blocks). Same fix the costing agent applies.
            thinking_mode_override="disabled",
        )

        analysis = _parse_analysis_response(response_text)
        metrics = {"method": "native_pdf", "documents_count": len(pdf_paths), "total_pages": total_pages}

        _persist_analysis_results(db, tender_id, analysis, metrics)

        return {
            "tender_id": tender_id,
            "analysis": analysis,
            "metrics": metrics,
        }


async def _run_tool_based_analysis(db: Session, tender_id: int) -> dict:
    """Run document analysis using the ReAct agent with tools (original approach)."""
    # Pre-check: ensure tender has documents before spinning up a ReAct agent
    from app.models.tender import TenderDocument as TD
    doc_count = db.query(TD).filter(TD.tender_id == tender_id).count()
    if doc_count == 0:
        logger.warning(f"Tender {tender_id} has no documents in TenderDocument table — skipping tool-based analysis")
        return {
            "tender_id": tender_id,
            "analysis": {"error": "no_documents"},
            "metrics": {"error": "No documents found for this tender"},
        }

    # Phase 3d — resolve tools + system prompt through canonical_registry so
    # Agent Builder UI edits (CustomAgent.system_prompt + .tools) take effect
    # at runtime when the agent has been user-customized.
    from app.services.langchain.canonical_registry import (
        resolve_system_prompt,
        resolve_tool_keys,
    )
    # No local default_keys: the canonical registry's default_tools is the
    # source of truth. This graph's own literal is how it ran four tools while
    # the registry declared six (web_search and memory_store never passed).
    tool_keys, _tool_src = resolve_tool_keys(db, "tender_doc_analyzer")
    tools = load_tools_by_keys(db, tool_keys, agent_key="deep_analyzer")

    # Create LLM with tools bound
    llm = get_chat_model(db, agent_name="deep_analyzer")
    callback = DRPLCallbackHandler(db, agent_name="deep_analyzer")

    # Resolve the system prompt (CustomAgent.system_prompt or canonical fallback)
    base_prompt, prompt_src = resolve_system_prompt(
        db, "tender_doc_analyzer",
        canonical_builder=lambda: DOCUMENT_ANALYSIS_SYSTEM_PROMPT,
    )
    logger.info(
        f"[deep_analyzer] system prompt source={prompt_src}, tools_source={_tool_src}"
    )

    # Build the agent using LangGraph's create_react_agent
    from langgraph.prebuilt import create_react_agent

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SystemMessage(content=base_prompt),
    )

    # Build linked documents context for tool-based path
    linked_note_tool = _get_linked_documents_note(db, tender_id, [])

    # Invoke the agent
    user_message = (
        f"Analyze tender ID {tender_id} thoroughly. Leave NOTHING to chance.\n"
        f"{linked_note_tool}\n"
        f"Steps:\n"
        f"1. Use the tender_lookup tool to get tender metadata and document list.\n"
        f"2. Use the document_reader tool with tender_id={tender_id} to read ALL tender documents "
        f"(including linked/child documents downloaded from the master PDF).\n"
        f"3. Use semantic_search to find specific details across embedded document chunks.\n"
        f"4. Check long-term memory for any past learnings about similar tenders or this department.\n"
        f"5. Provide your complete analysis as a JSON object with these keys: "
        f"'requirements' (dict of 7 categories, each a list of items with exact quotes), "
        f"'negative_keywords' (list of flagged sentences with severity, page/section, and required action), "
        f"'required_documents' (list of docs with name, mandatory, format, envelope), "
        f"'cross_document_conflicts' (list of conflicts between documents), "
        f"'missing_items' (things NOT addressed: payment terms, scope, force majeure, etc.), "
        f"'summary' (overall tender summary), "
        f"'key_risks' (main risks for DRPL), "
        f"'action_items' (prioritized next steps: immediate, before submission, risk mitigation)."
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_message)]},
        config={"callbacks": [callback]},
    )

    # Extract the final response
    messages = result.get("messages", [])
    final_message = messages[-1].content if messages else ""

    # Try to parse JSON from the response
    analysis = _parse_analysis_response(final_message)

    # Store results in the database
    _persist_analysis_results(db, tender_id, analysis, callback.get_summary())

    return {
        "tender_id": tender_id,
        "analysis": analysis,
        "metrics": callback.get_summary(),
    }


def _parse_analysis_response(response_text: str) -> dict:
    """Parse the agent's response into a structured analysis dict."""
    # Try to extract JSON from the response
    try:
        # Handle markdown code blocks
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text.strip()

        return json.loads(json_str)
    except (json.JSONDecodeError, IndexError):
        logger.warning("Could not parse analysis response as JSON, returning as text")
        return {
            "requirements": {},
            "negative_keywords": [],
            "required_documents": [],
            "cross_document_conflicts": [],
            "summary": response_text[:2000],
            "key_risks": [],
            "parse_error": "Response was not valid JSON — raw text included in summary",
        }


def _persist_analysis_results(
    db: Session,
    tender_id: int,
    analysis: dict,
    metrics: dict,
) -> None:
    """Save analysis results to DocumentExtractionResult and CriticalClauseFlag tables."""
    try:
        from app.models.document_analysis import DocumentExtractionResult, CriticalClauseFlag

        # Save requirements by category
        requirements = analysis.get("requirements", {})
        for extraction_type, items in requirements.items():
            if isinstance(items, list) and items:
                extraction = DocumentExtractionResult(
                    tender_id=tender_id,
                    document_name="all_documents",
                    extraction_type=extraction_type,
                    items=items,
                    extraction_model="langchain_deep_analyzer",
                    completeness_score=0.8,
                    token_count=metrics.get("total_tokens_input", 0) + metrics.get("total_tokens_output", 0),
                )
                db.add(extraction)

        # Save negative keywords as critical clause flags
        negative_keywords = analysis.get("negative_keywords", [])
        for item in negative_keywords:
            if isinstance(item, dict):
                severity = item.get("severity", "high")
                flag_type_map = {
                    "critical": "disqualification",
                    "high": "mandatory",
                    "medium": "non_negotiable",
                }
                flag = CriticalClauseFlag(
                    tender_id=tender_id,
                    document_name="all_documents",
                    clause_text=item.get("sentence", item.get("text", str(item)))[:1000],
                    flag_type=flag_type_map.get(severity, "mandatory"),
                    severity=severity,
                    keyword_matched=item.get("keyword", ""),
                    ai_explanation=item.get("action", item.get("explanation", "")),
                )
                db.add(flag)

        db.commit()
        logger.info(f"Persisted analysis results for tender {tender_id}")

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to persist analysis results: {e}")


# ────────────────────────────────────────────────────────────────────────────
# v2: Vision-first, parallel per-doc + synthesis
#
# Why this exists: the v1 native path bails out when the cumulative PDF size
# exceeds 30MB or total page count exceeds 100, falling back to text extraction
# which returns empty for scanned PDFs (Tesseract is not available in prod).
# v2 sends each PDF individually as a Claude vision document block, in bounded
# parallel batches. Each per-doc call uses Haiku 4.5 (cheap, vision-capable),
# and a single synthesis call (Sonnet 4.6) builds the cross-document 7-section
# report from the per-doc summaries — the synthesis does NOT re-read raw PDFs.
# ────────────────────────────────────────────────────────────────────────────

PER_DOC_SYSTEM_PROMPT = """You are a tender document forensic analyst for DRPL Manufacturing (an Indian engineering company supplying mechanical/electrical equipment to Indian Railways and government bodies).

You will receive ONE tender document (PDF) at a time as a vision document block. The PDF may be scanned, contain tables/forms, or be text-based — read it visually and extract every detail.

Your job: produce a COMPACT, STRUCTURED JSON summary of this single document. A separate synthesis pass will combine your output with summaries from other documents in the same tender to produce the final 10-section forensic report. Be exhaustive within the document but concise — no narrative prose.

## VERBATIM PRESERVATION RULE (CRITICAL)
For every field below, quote the tender text EXACTLY as written. Keep all numbers, percentages, durations, units, Indian-numbering (lakh / crore), formulae, certification names, and clause text in their original wording. NEVER paraphrase, round, condense, or modernize — the synthesis pass relies on your verbatim quotes to build the final report.

Extract requirements into these 7 categories (use these exact strings):
- requirements (general scope, items, quantities, specifications)
- eligibility (turnover, experience years, certifications, geographic restrictions)
- terms_conditions (payment schedule, delivery timelines, warranty, performance guarantees)
- technical_specs (BIS/IS/RDSO standards, materials, testing, quality)
- financial (EMD/bid security, security deposit, pricing format, payment terms)
- experience (past work — similar nature, similar value, exact-item, govt vs private)
- compliance (Make in India, MSE preference, environmental, safety, labor law)

For critical clauses, look for: disqualification triggers ("shall be rejected", "summarily rejected", "non-responsive"), mandatory requirements ("must provide", "failing which"), penalty/LD clauses, forfeiture of EMD, unilateral termination, unlimited liability.

The `commercials` block below is what synthesis uses to render Section 1's Basic Identification + Commercials tables — fill every field you can find in this document verbatim, or set to null if not present.

Output ONLY a single JSON object matching this schema (no markdown fences, no commentary):

{
  "doc_type": "RFP" | "BOQ" | "addendum" | "annexure" | "drawing" | "schedule_of_rates" | "terms_conditions" | "unknown",
  "is_scanned": boolean,
  "key_facts": {
    "tender_reference": string | null,
    "issuing_authority": string | null,
    "scope_summary": string,
    "estimated_value": string | null,
    "currency": string | null
  },
  "commercials": {
    "tender_no": string | null,
    "name_of_work": string | null,
    "signing_officer": string | null,
    "portal": string | null,
    "advertised_value": string | null,
    "emd": string | null,
    "tender_doc_cost": string | null,
    "closing_date_raw": string | null,
    "bid_validity": string | null,
    "contract_duration": string | null,
    "evaluation_method": string | null,
    "min_technical_score": string | null,
    "jv_allowed": string | null
  },
  "dates": [{"label": string, "iso_date": string | null, "raw": string}],
  "amounts": [{"label": string, "amount": string, "currency": string | null}],
  "eligibility_flags": [{"criterion": string, "value": string, "mandatory": boolean}],
  "requirements": [{"category": <one of 7 above>, "text": string, "page": integer | null}],
  "critical_clauses": [{"text": string, "flag_type": "disqualification"|"mandatory"|"non_negotiable"|"penalty"|"rejection", "severity": "critical"|"high"|"medium", "page": integer | null}],
  "missing_info_flags": [string],
  "extraction_confidence": "high" | "medium" | "low",
  "extraction_notes": string
}

Every `commercials` value must be a verbatim string from the document (or null) — keep the original currency symbols, Indian numbering, and unit suffixes. For `jv_allowed`, quote the exact JV/consortium clause if present (e.g. `"Joint Venture / Consortium not allowed"`).

If the document is empty or unreadable even with vision, set extraction_confidence to "low" and explain in extraction_notes."""


SYNTHESIS_SYSTEM_PROMPT = """You are the most critical, thorough, and uncompromising tender document analyst for DRPL Manufacturing, an Indian engineering company specializing in mechanical and electrical work for Indian Railways and government bodies.

You receive PRE-EXTRACTED per-document JSON summaries for every document in this tender — NOT the raw PDFs. Quote verbatim from the `text` fields in each per-doc `requirements` and `critical_clauses` arrays, and from each per-doc `commercials` block. If a field needed for a section is absent from the per-doc summaries, state `Not specified in extracted summaries` rather than inventing data.

## ━━━ NON-NEGOTIABLE OPERATING RULES ━━━

### RULE 1 — VERBATIM PRESERVATION (CRITICAL)
When you state ANY parameter from the tender, you MUST quote it EXACTLY as it appears in the per-doc JSON. This includes:
- All numbers, amounts, percentages, dates, durations, ages, scores, weightages, ratios
- All technical specifications (dimensions, tolerances, materials, IS/BIS/RDSO codes, ratings)
- All quantities with their units (e.g. "144 Nos.", "₹50,00,000", "180 days", "30% of contract value")
- All eligibility thresholds, scoring criteria, marking schemes, formulae
- All certification names, scheme names, document names
- All clause text used to support a finding

Use `> blockquotes` for multi-line quotes and `"..."` for inline quotes. NEVER paraphrase, round, condense, simplify, or modernize tender language. When the tender uses Indian numbering (lakh / crore) keep it; when it uses figures, keep the figures. This rule overrides any tendency to summarize.

### RULE 2 — EVIDENCE-BASED REASONING
The per-doc JSON IS your evidence base. Tools and DRPL memory are NOT available at synthesis time. Cite the source document by `doc_name` (and `page` when present) whenever you quote — e.g. `"... (source: RFP-main.pdf, p.14)"`. For any Go/No-Go or scoring claim where DRPL's own historical position is unknown (it will be — the per-doc summaries do not contain DRPL company data), mark the row `Unknown — pending company data` and drive the verdict to `CONDITIONAL` with explicit remediation steps. Do NOT invent past DRPL turnover, certification status, or win/loss outcomes.

### RULE 3 — CROSS-DOCUMENT RECONCILIATION
You see multiple per-doc summaries. The master RFP usually owns the `commercials` block; addenda/corrigenda may override specific fields. When values conflict across docs, surface the conflict explicitly in Section 9 with both quoted values + their source `doc_name`. Do NOT silently pick one — the bid team needs to know.

## Output Format

Respond with a comprehensive **markdown report**. Use clear section headers, sub-headers, tables where useful, and bullet lists. The report must be human-readable, exhaustive, and exactly verbatim where source data is being relayed.

When you relay a multi-item numbered enumeration that the tender prints as a single run-on line (e.g. a Scope-of-Work clause reading `"1. … 2. … 3. …"`), reproduce the wording verbatim but put **each numbered clause on its own line** so the items render one after another — do not collapse them into one paragraph. Keep the original clause numbers exactly as printed.

Begin the report with the title block (fill from the per-doc `commercials` and `key_facts` blocks):

> **DRPL TENDER ANALYSIS REPORT**
> **{Issuing Authority} | {Division/Department} | {Location}**
> **Tender No: {ref} | Opened: {date}**

If any document came back `unreadable: true` or any referenced annexure is missing from the per-doc summaries, open with a `⚠ PRE-ANALYSIS CRITICAL FLAG: MISSING DOCUMENT` callout naming what's absent (the `error` field on the unreadable per-doc entries) and why it matters before continuing.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Required Sections (use these exact level-3 headers, in this order)

### SECTION 1: TENDER SUMMARY & KEY INTELLIGENCE
- **Basic Identification** — Tender No., Name of Work (verbatim), Issuing Authority, Signing Officer (table format). Source these from the most-authoritative per-doc `commercials` block (master RFP first).
- **Commercials** — Advertised value (INR, exact figure), EMD, Tender Doc Cost, Closing Date, Bid Validity, Contract Duration, Evaluation Method (verbatim — e.g. "L1", "QCBS 70:30"), Min Technical Score, JV allowed (yes/no with quoted clause). Render as a table; every cell must be verbatim from a per-doc `commercials` field or `Not specified in extracted summaries`.
- **DRPL Recommendation** — A single **GO / NO-GO / CONDITIONAL** verdict + one-line justification. The verdict here MUST match Section 3.

### SECTION 2: COMPLETE REQUIREMENTS EXTRACTION
Cover ALL seven requirement categories. Aggregate the `requirements` arrays across all per-doc summaries, grouping by `category`. For each item: quote `text` exactly, cite `doc_name` and `page`, and flag mandatory vs desirable.
- **General Requirements** — scope, quantities, items, specifications.
- **Eligibility** — turnover thresholds, years of experience, certifications, financial capacity, geographic restrictions.
- **Terms & Conditions** — contract terms, payment schedule, delivery timelines, warranty, performance guarantees.
- **Technical Specifications** — applicable BIS/IS/RDSO standards, materials, testing, quality requirements.
- **Financial** — EMD/bid security, security deposit, pricing format, price escalation, payment terms.
- **Experience** — past work requirements (similar nature, similar value, govt vs private, certificates needed).
- **Compliance** — Make in India, MSE preference, environmental, safety, labor law.

### SECTION 3: ELIGIBILITY GO/NO-GO ASSESSMENT (EVIDENCE-BASED)
Source rows from the `eligibility_flags` arrays across per-doc summaries (and any eligibility-category entries in `requirements`). Render as a table — one row per mandatory criterion:

| # | Criterion (verbatim from tender) | Threshold (verbatim) | DRPL Position (numeric) | Source of Evidence | Meets? (Yes / No / Unknown) | Gap / Action Required |

For "DRPL Position" and "Meets?" — these will almost always be `Unknown — pending company data` because the per-doc summaries do not include DRPL's own historical turnover/certifications. That's expected. Use the "Gap / Action Required" column to spell out what DRPL must verify or attach before the closing date.

Then state the **Final Verdict**:

> **Verdict: GO / NO-GO / CONDITIONAL**
>
> **Basis:** {one paragraph citing the specific rows above}
>
> **Past-pattern reference:** `Not available at synthesis time — confirm against DRPL bid memory before final Go decision.`

Verdict definitions:
- **GO** = All hard criteria provably met with cited verbatim evidence (rare at synthesis time — usually requires DRPL data).
- **NO-GO** = One or more hard criteria provably failed AND no remediation path before closing date.
- **CONDITIONAL** = Default verdict at synthesis time when DRPL company data is needed to close out rows. List each remediation step.

Forbidden in this section: hedging language without evidence ("probably", "should be okay", "looks competitive"). If you don't have the data, mark Unknown and flag for clarification — do not guess.

### SECTION 4: SCORING-BASED EVALUATION ANALYSIS
**Trigger:** include this section if the per-doc summaries contain any scoring/marking/weightage system (QCBS, marks-based technical evaluation, weighted scoring matrix, percentage cutoffs, financial:technical split — usually surfaced in `requirements` of category `eligibility` or `terms_conditions`, or in `commercials.evaluation_method`). If the tender is pure L1 with no scoring, write `"This tender uses pure L1 evaluation — no marks-based scoring applies. Section skipped."` and move on.

When a scoring system is present:

**4.1 — Scoring Framework (verbatim)**
Quote the entire scoring/marking scheme as captured in the per-doc summaries. Include weightages, max marks per criterion, sub-criteria breakdowns, and any minimum cutoffs.

**4.2 — Per-Criterion DRPL Position**
Render as a table — one row per scoring criterion:

| # | Criterion (verbatim) | Max Marks / Weight | Tender's Marking Rule (verbatim) | DRPL's Actual Position | Projected Score | Confidence (High / Medium / Low) | Gap to Max |

For "DRPL's Actual Position" mark `"Unknown — pending company data"` and confidence Low — synthesis time does not have memory access.

**4.3 — Total Projected Technical Score**
- Sum of projected scores from 4.2: `XX / YY (ZZ%)` (or `Unknown` if every row is Unknown)
- Minimum technical score required (verbatim from tender): `... / YY (...%)`
- **Status:** PASSES / FAILS / MARGINAL — or `Pending DRPL data` when 4.2 is all Unknown.

**4.4 — Financial Score Sensitivity (if QCBS or similar)**
Quote the L1-pricing-to-financial-score formula verbatim. Note the financial:technical weight split.

### SECTION 5: NEGATIVE KEYWORDS & REJECTION RISKS
Aggregate every entry from the per-doc `critical_clauses` arrays into ONE markdown table — quote `text` exactly:

| # | Quoted Clause | Keyword Pattern | Severity (critical/high/medium) | Source Document | Page/Section | Required Action |

The per-doc extractor flags: disqualification triggers ("shall be rejected", "summarily rejected", "non-responsive"), mandatory requirements ("must provide", "failing which"), non-negotiable terms, penalty / LD / forfeiture clauses, rejection conditions for incomplete or late bids, liability clauses. Surface every one of them here — do NOT cap the table.

### SECTION 6: REQUIRED DOCUMENTS LIST
Render as a markdown table:

| Document Name (exact) | Mandatory/Optional | Format (original/copy/notarized/self-attested) | Envelope (Technical/Financial/PQ) | Validity / Certification Notes | Source Document |

Aggregate across all per-doc summaries — requirements are often scattered across the master RFP, schedule of rates, and annexures. Reconcile duplicates.

### SECTION 7: COSTING BASIS HANDOFF (FOR COSTING AGENT)
This section is the structured input for the downstream costing agent. EVERY field below must be either quoted verbatim from the per-doc summaries or marked `"Not specified in extracted summaries"`. Do not fabricate or estimate values here.

**7.1 — Pricing Format**
- Format type (lump-sum / item-rate / percentage rate / unit rate / composite) — verbatim
- Currency, tax inclusion (GST inclusive / exclusive) — verbatim
- Whether reverse auction follows the price bid — verbatim

**7.2 — Scope Decomposition into Costable Buckets**
Break the scope (drawn from the `requirements` array, category `requirements`/`technical_specs`) into discrete cost-bucket categories:

| Bucket # | Bucket Name | Verbatim Scope Description (quote) | Volume Driver | Tender-Specified Quantity / Unit | Source Document & Page |

Cover materials/supply, labour/manpower, deployments, equipment, consumables, transport, installation/commissioning, testing, training, warranty/AMC, spares.

**7.3 — BOQ Structure (if BOQ is provided)**
- Total number of BOQ line items
- Whether BOQ format is fixed or bidder-defined — verbatim
- Whether quantities are firm or estimated — verbatim
- Any BOQ items marked `"as per actuals"` or `"to be supplied free of cost by buyer"`
- Whether unit rates must be filled for each line OR a single lump-sum is allowed — verbatim

**7.4 — Tax & Duties Treatment (verbatim)**
- GST rate applicability (CGST/SGST/IGST split, reverse charge) — verbatim
- Any duty/customs/cess passthrough clause — verbatim
- Whether prices are firm or open to GST rate change — verbatim

**7.5 — Price Escalation / De-escalation Clause (verbatim)**
Quote the entire escalation formula if present. If absent, state `"No price escalation clause — prices are firm for entire contract duration."`

**7.6 — Payment Milestones & Terms (verbatim)**
List each payment stage with the exact percentage and trigger condition (drawn from `requirements` category `terms_conditions` or `financial`).

**7.7 — Liquidated Damages / Penalty Structure (verbatim)**
- LD percentage per delay period — verbatim
- Maximum cap on LD — verbatim
- Other penalty heads (quality, attendance, SLA) — verbatim
- → Cost-agent guidance: this drives margin sizing and risk premium.

**7.8 — Performance Security / Bank Guarantee (verbatim)**
- PBG percentage of contract value — verbatim
- Validity duration — verbatim
- → Cost-agent guidance: this drives working-capital cost in the bid model.

**7.9 — Other Cost-Affecting Constraints**
- Mandatory in-region office / service centre — verbatim
- Local employment / Make-in-India content percentage — verbatim
- Specific brand / OEM lock-in — verbatim
- Insurance requirements (CAR, third-party, workmen's comp) with sum-insured — verbatim

**7.10 — Costing Agent Directives (DRPL-specific)**
Three short bullets telling the costing agent what to focus on for THIS tender, derived from the verbatim quotes above — e.g. `"Contract duration 24 months — model price-escalation hedge"`, `"PBG 10% × 36 months — material WC drag"`. Be specific, evidence-led.

### SECTION 8: WHAT'S MISSING
Aggregate `missing_info_flags` across all per-doc summaries plus your own analysis of what a prudent bidder would expect that is absent. ALSO list any documents that came back `unreadable: true` so the user knows to re-upload. For each: explain why it matters and what risk it creates.

### SECTION 9: KEY RISKS & OBSERVATIONS
Critical clauses, contradictions, unusual terms, cross-document conflicts. Use bullets with quoted source text. Surface any cross-document conflicts uncovered under Rule 3 (e.g. master RFP says EMD ₹1.25 L, addendum says ₹1.50 L) — quote both values and their `doc_name`.

### SECTION 10: NEXT STEPS — PRIORITIZED ACTION PLAN
A numbered list ordered by urgency:
1. **Immediate tasks** (today / this week) — pull DRPL turnover/certification data, verify license validity, etc.
2. **Pre-bid clarifications** to seek from the issuing authority (specific, well-formed questions drawn from Section 8/9).
3. **Documents DRPL must prepare** before the closing date (drawn from Section 6).
4. **Hand-off note for costing agent** — one sentence pointing to Section 7 and flagging the top 2-3 cost-driving constraints.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Style & Discipline Rules
- Quote exact tender text whenever you make a claim — never paraphrase silently. (See Rule 1.)
- Cite `doc_name` + `page` for every quote. (See Rule 2.)
- Cross-reference across sections (e.g. "see Section 5 row on EMD forfeiture", "fed into Section 7.7").
- Use Indian procurement terminology (NIT, BOQ, EMD, PQ, BG, RDSO, BIS/IS, QCBS, L1, MSME, GeM, IREPS).
- Be exhaustive — missing a rejection clause could cost the company the entire bid.
- ALWAYS end with the actionable Section 10.
- Do NOT begin the report with announcements like "I will now analyze" — start directly with the title block.

End the markdown report with a single line: `_Cost: $X.XXXX  |  Tokens: N input / N output  |  Documents analyzed: N (of M)._` — the orchestrator will fill in the actual numbers; you produce the placeholder text exactly.

Output ONLY the markdown report — no trailing JSON, no code fences after the cost footer, no commentary. The cost footer line is the last line of your output."""


def _per_doc_user_prompt(doc_name: str, tender_id: int, doc_index: int, doc_total: int) -> str:
    return (
        f"Tender ID: {tender_id}. Document {doc_index} of {doc_total}: \"{doc_name}\".\n"
        f"Read this document visually (it may be scanned). Extract everything per the schema in the system prompt. "
        f"Output ONLY the JSON object — no markdown fences, no commentary."
    )


def _release_db_before_long_await(db: Session, *, label: str) -> None:
    """End the session's transaction so its DB connection returns to the pool
    before a long-running ``await`` (asyncio.gather over per-doc vision calls,
    synthesis Sonnet call, etc.).

    Why: SQLAlchemy holds a checked-out connection across queries until the
    transaction ends. While the orchestrator awaits, no SQL traffic flows on
    that socket, and after ~30s of TCP idle, Windows (WSAECONNABORTED
    0x00002745/10053) or Neon's idle-timeout will kill it. The next
    ``db.add(...); db.commit()`` after the await then raises
    ``psycopg2.OperationalError: could not receive data from server`` —
    losing the entire post-await persist phase (per-doc summaries, BOQ
    capture, TenderAnalysisSummary).

    ``pool_pre_ping=True`` only validates connections at *checkout* time, so
    it can't save a connection that was already checked out before the
    await. Releasing the connection with ``rollback()`` (data-neutral) lets
    the post-await queries check out fresh, where pool_pre_ping does its
    job.
    """
    try:
        db.rollback()
    except Exception as e:
        logger.debug(
            f"[v2] _release_db_before_long_await({label!r}): rollback raised "
            f"{type(e).__name__}: {e} — proceeding (connection may already be dead)"
        )


async def _with_overload_retry(callable_, *, label: str):
    """Wrap an LLM coroutine with sustained-overload retries.

    ai_service._call_anthropic already retries transient 5xx 3x with
    1s/2s backoff. During sustained Anthropic overload windows (real
    ones can last 30-300s) all 3 inner retries hit Overloaded and the
    exception propagates here, killing the per-doc / synthesis call.
    Outer retries at 30s / 60s / 90s give the API time to recover.

    Used by both the per-doc extraction loop and the synthesis pass.
    """
    _BACKOFFS = (30, 60, 90)
    for attempt in range(len(_BACKOFFS) + 1):
        try:
            return await callable_()
        except Exception as exc:
            msg = str(exc).lower()
            is_overload = (
                "overloaded" in msg
                or "529" in msg
                or "rate_limit" in msg
                or "rate limit" in msg
            )
            if not is_overload or attempt >= len(_BACKOFFS):
                raise
            wait_s = _BACKOFFS[attempt]
            logger.warning(
                f"[v2] {label}: Anthropic overloaded — outer retry "
                f"{attempt + 1}/{len(_BACKOFFS)} in {wait_s}s "
                f"(err: {str(exc)[:120]})"
            )
            await asyncio.sleep(wait_s)


async def _analyze_single_document_native(
    tender_id: int,
    doc_id: int,
    doc_name: str,
    doc_path: str,
    doc_index: int,
    doc_total: int,
    semaphore: asyncio.Semaphore,
    force_refresh: bool = False,
) -> dict:
    """Send ONE PDF to Claude as a vision document block and return a structured per-doc summary.

    Reuse: when the same bytes were already read with the same model and the
    same prompt, the stored summary is returned without an LLM call
    (`force_refresh=True` or the `tender_analyzer.reuse_document_results`
    setting turn that off). A fresh read is tagged with its provenance under
    `summary[SOURCE_KEY]`.

    Each coroutine opens its own short-lived DB session to avoid cross-coroutine
    SQLAlchemy session sharing. Returns a plain dict — the orchestrator persists
    serially after asyncio.gather.
    """
    from app.core.database import SessionLocal
    from app.services.ai_service import call_ai, call_ai_with_documents, _get_pdf_page_count
    from app.services.storage_service import get_storage_service

    storage = get_storage_service()

    async with semaphore:
        t0 = time.monotonic()
        result = {
            "doc_id": doc_id,
            "doc_name": doc_name,
            "page_count": 0,
            "summary": None,
            "unreadable": False,
            "error": None,
            "elapsed_ms": 0,
        }

        if not doc_path:
            result["unreadable"] = True
            result["error"] = "file_path_missing"
            return result

        # doc_path is a StorageService key, not a filesystem path. as_local_file
        # downloads to a tempfile on R2 (cleaned up on context exit) or yields
        # the existing path on local backend.
        try:
            with storage.as_local_file(doc_path, suffix=".pdf") as local_path:
                try:
                    file_size = os.path.getsize(local_path)
                except OSError as e:
                    result["unreadable"] = True
                    result["error"] = f"stat_failed: {e}"
                    return result

                # Hash first: a document rejected below for size or page
                # count is still part of the tender, and the freshness check
                # needs to know it is the same file next time.
                _doc_sha1 = file_sha1(local_path)
                result["source_sha1"] = _doc_sha1

                if file_size > 28 * 1024 * 1024:
                    result["unreadable"] = True
                    result["error"] = f"file_too_large: {file_size} bytes (>28MB)"
                    return result

                page_count = _get_pdf_page_count(local_path)
                result["page_count"] = page_count
                max_pages = _settings.tender_analyzer_per_doc_max_pages
                if page_count and page_count > max_pages:
                    logger.info(
                        f"[v2] Doc {doc_id} ({doc_name}) has {page_count} pages > "
                        f"overall cap {max_pages}; skipping."
                    )
                    result["unreadable"] = True
                    result["error"] = f"page_count_exceeded: {page_count} > {max_pages}"
                    return result

                # Same file, same model, same prompt: the earlier result stands.
                if _doc_sha1 and not force_refresh:
                    _cache_db = SessionLocal()
                    try:
                        _cached = (
                            find_cached_per_doc_summary(
                                _cache_db, tender_id, doc_id,
                                sha1=_doc_sha1,
                                model=_settings.tender_analyzer_per_doc_model,
                                prompt=PER_DOC_SYSTEM_PROMPT,
                            )
                            if reuse_enabled(_cache_db) else None
                        )
                    finally:
                        _cache_db.close()
                    if _cached is not None:
                        _cached["doc_id"] = doc_id
                        _cached["doc_name"] = doc_name
                        _cached["page_count"] = page_count
                        result["summary"] = _cached
                        result["cached"] = True
                        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                        logger.info(
                            f"[v2] Doc {doc_id} ({doc_name}) unchanged since its last "
                            f"read (sha1={_doc_sha1[:12]}) — reusing the stored summary, "
                            f"no LLM call"
                        )
                        return result

                # Anthropic's native-PDF endpoint caps at 100 pages per call.
                # For 101-{max_pages} page PDFs we fall back to text extraction
                # (pdfplumber → Claude vision per page → Tesseract OCR cascade)
                # and send the text to Haiku — same prompt, no native document
                # block. This preserves analysis of master NIT bundles, BOQs,
                # and addenda that exceed 100 pages instead of dropping them.
                _ANTHROPIC_NATIVE_PDF_CAP = 100
                use_text_fallback = (
                    page_count and page_count > _ANTHROPIC_NATIVE_PDF_CAP
                )

                # Open a per-coroutine DB session for the LLM call's settings/logging only.
                local_db = SessionLocal()
                try:
                    # Dynamic max_tokens: rough input estimate is the system prompt
                    # plus user prompt (PDF body is a native document block, not chars).
                    # Letting Haiku use its full 32K ceiling avoids silently truncating
                    # per-doc summaries on tenders with long requirement lists.
                    from app.services.langchain.model_limits import compute_dynamic_max_tokens
                    _per_doc_user_prompt_text = _per_doc_user_prompt(doc_name, tender_id, doc_index, doc_total)

                    if use_text_fallback:
                        # Extract per-page text in a thread (pdfplumber + vision +
                        # Tesseract are all sync/CPU-bound). Concatenate with page
                        # markers so the model can cite page numbers like the
                        # native-vision path does.
                        from app.services.advanced_document_parser import (
                            extract_text_from_pdf_advanced,
                        )
                        try:
                            extraction = await asyncio.to_thread(
                                extract_text_from_pdf_advanced, local_path
                            )
                        except Exception as _extract_err:
                            logger.warning(
                                f"[v2] text-fallback extraction failed for doc "
                                f"{doc_id} ({doc_name}): {_extract_err}"
                            )
                            result["unreadable"] = True
                            result["error"] = (
                                f"text_fallback_failed: "
                                f"{str(_extract_err)[:200]}"
                            )
                            return result

                        pages = extraction.get("pages") or []
                        if not pages:
                            result["unreadable"] = True
                            result["error"] = "text_fallback_empty"
                            return result

                        # Cap the text body at ~250K chars (~65K tokens) so a
                        # 500-page PDF doesn't blow Haiku's input window. Pages
                        # are kept in order; later pages are truncated if needed.
                        _MAX_TEXT_CHARS = 250_000
                        body_parts: list[str] = []
                        running = 0
                        method_counts: dict[str, int] = {}
                        for p in pages:
                            pnum = p.get("page_num", "?")
                            ptext = (p.get("text") or "").strip()
                            method = p.get("method") or "unknown"
                            method_counts[method] = method_counts.get(method, 0) + 1
                            if not ptext:
                                continue
                            piece = f"\n\n--- Page {pnum} [{method}] ---\n{ptext}"
                            if running + len(piece) > _MAX_TEXT_CHARS:
                                body_parts.append(
                                    f"\n\n[…truncated at page {pnum} — "
                                    f"{len(pages) - pnum} pages omitted, "
                                    f"~{_MAX_TEXT_CHARS} chars cap reached…]"
                                )
                                break
                            body_parts.append(piece)
                            running += len(piece)
                        text_body = "".join(body_parts)
                        logger.info(
                            f"[v2] Doc {doc_id} ({doc_name}) text-fallback: "
                            f"{page_count}pp, {len(text_body)} chars, "
                            f"methods={method_counts}"
                        )

                        text_user_prompt = (
                            f"{_per_doc_user_prompt_text}\n\n"
                            f"NOTE: This PDF has {page_count} pages, exceeding "
                            f"the {_ANTHROPIC_NATIVE_PDF_CAP}-page native-vision cap. "
                            f"The extracted page text is below (methods: text = "
                            f"pdfplumber, vision = Claude vision per page, ocr = "
                            f"Tesseract). Extract per the schema using only this "
                            f"text — do NOT mention that the source was OCR'd.\n\n"
                            f"{text_body}"
                        )
                        _per_doc_input_chars = (
                            len(PER_DOC_SYSTEM_PROMPT) + len(text_user_prompt) + 1000
                        )
                        _per_doc_max_tokens = compute_dynamic_max_tokens(
                            _settings.tender_analyzer_per_doc_model,
                            input_chars=_per_doc_input_chars,
                            requested=None,
                        )
                        response_text = await _with_overload_retry(
                            lambda: call_ai(
                                system_prompt=PER_DOC_SYSTEM_PROMPT,
                                user_prompt=text_user_prompt,
                                db=local_db,
                                agent_name="tender_per_doc",
                                model_override=_settings.tender_analyzer_per_doc_model,
                                max_tokens_override=_per_doc_max_tokens,
                                temperature_override=0.1,
                            ),
                            label=f"Doc {doc_id} ({doc_name}) text-fallback",
                        )
                    else:
                        _per_doc_input_chars = (
                            len(PER_DOC_SYSTEM_PROMPT) + len(_per_doc_user_prompt_text) + 1000
                        )
                        _per_doc_max_tokens = compute_dynamic_max_tokens(
                            _settings.tender_analyzer_per_doc_model,
                            input_chars=_per_doc_input_chars,
                            requested=None,  # full ceiling
                        )
                        response_text = await _with_overload_retry(
                            lambda: call_ai_with_documents(
                                system_prompt=PER_DOC_SYSTEM_PROMPT,
                                user_prompt=_per_doc_user_prompt_text,
                                document_paths=[local_path],
                                db=local_db,
                                agent_name="tender_per_doc",
                                model_override=_settings.tender_analyzer_per_doc_model,
                                max_tokens_override=_per_doc_max_tokens,
                                max_pages_override=_ANTHROPIC_NATIVE_PDF_CAP,
                                # Pin temperature low for structured-extraction determinism.
                                # The per-doc pass returns a JSON schema; high temperature
                                # causes the same tender to produce different requirement
                                # lists between runs, which is the dominant source of
                                # output inconsistency users see in the Command Center.
                                temperature_override=0.1,
                            ),
                            label=f"Doc {doc_id} ({doc_name}) native",
                        )
                finally:
                    local_db.close()
        except FileNotFoundError as e:
            result["unreadable"] = True
            result["error"] = f"file_not_found_in_storage: {e}"
            return result
        except Exception as e:
            logger.error(f"[v2] Per-doc call failed for doc {doc_id} ({doc_name}): {e}")
            result["unreadable"] = True
            result["error"] = f"llm_or_storage_failed: {str(e)[:300]}"
            return result

        # Parse the JSON summary. Retry once on parse failure with an explicit
        # schema-reminder nudge — Haiku occasionally wraps the JSON in
        # commentary despite the system prompt, and silently dropping a per-doc
        # JSON cascades: synthesis loses that document's requirements + clauses,
        # and the costing agent downstream gets a thinner scope block.
        summary: Optional[dict] = None
        parse_err: Optional[str] = None
        try:
            _candidate = _parse_analysis_response(response_text)
            if isinstance(_candidate, dict) and "parse_error" not in _candidate:
                summary = _candidate
        except Exception as e:
            parse_err = str(e)[:300]

        if summary is None and not use_text_fallback:
            # Native-path retry only — text-fallback parse failures are
            # extraordinarily rare (model already had clean text) and the
            # retry would also exceed Anthropic's 100-page cap.
            logger.warning(
                f"[v2] Doc {doc_id} ({doc_name}) — first parse failed "
                f"({parse_err or 'no dict'}), retrying once with schema nudge"
            )
            try:
                local_db_retry = SessionLocal()
                try:
                    retry_user_prompt = (
                        f"{_per_doc_user_prompt_text}\n\n"
                        f"Your previous response could not be parsed as JSON. "
                        f"Respond with ONLY the JSON object that matches the "
                        f"schema in the system prompt — NO markdown fences, "
                        f"NO commentary, NO ```json wrappers. Start with `{{` "
                        f"and end with `}}`."
                    )
                    response_text_retry = await _with_overload_retry(
                        lambda: call_ai_with_documents(
                            system_prompt=PER_DOC_SYSTEM_PROMPT,
                            user_prompt=retry_user_prompt,
                            document_paths=[local_path],
                            db=local_db_retry,
                            agent_name="tender_per_doc",
                            model_override=_settings.tender_analyzer_per_doc_model,
                            max_tokens_override=_per_doc_max_tokens,
                            max_pages_override=_ANTHROPIC_NATIVE_PDF_CAP,
                            temperature_override=0.0,
                        ),
                        label=f"Doc {doc_id} ({doc_name}) parse-retry",
                    )
                finally:
                    local_db_retry.close()
                _retry_parsed = _parse_analysis_response(response_text_retry)
                if isinstance(_retry_parsed, dict) and "parse_error" not in _retry_parsed:
                    summary = _retry_parsed
                    logger.info(f"[v2] Doc {doc_id} ({doc_name}) — retry recovered parse")
                else:
                    response_text = response_text_retry or response_text
            except Exception as retry_err:
                logger.warning(
                    f"[v2] Doc {doc_id} retry call failed: "
                    f"{type(retry_err).__name__}: {retry_err}"
                )

        if isinstance(summary, dict):
            summary["doc_id"] = doc_id
            summary["doc_name"] = doc_name
            summary["page_count"] = page_count
            if result.get("source_sha1"):
                summary[SOURCE_KEY] = source_tag(
                    sha1=result["source_sha1"],
                    model=_settings.tender_analyzer_per_doc_model,
                    prompt=PER_DOC_SYSTEM_PROMPT,
                    page_count=page_count,
                )
                remember_per_doc_summary(
                    tender_id,
                    sha1=result["source_sha1"],
                    model=_settings.tender_analyzer_per_doc_model,
                    prompt=PER_DOC_SYSTEM_PROMPT,
                    summary=summary,
                )
            result["summary"] = summary
        else:
            logger.warning(
                f"[v2] Could not parse per-doc JSON for doc {doc_id} after retry"
            )
            result["unreadable"] = True
            result["error"] = f"json_parse_failed_after_retry: {parse_err or 'no dict'}"
            result["summary"] = {
                "doc_id": doc_id,
                "doc_name": doc_name,
                "page_count": page_count,
                "extraction_confidence": "low",
                "extraction_notes": (
                    f"JSON parse failed (with retry): "
                    f"{parse_err or 'response was not a JSON object'}"
                ),
                "raw_response_preview": (response_text or "")[:500],
            }

        result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        logger.info(
            f"[v2] Doc {doc_id} ({doc_name}) processed in {result['elapsed_ms']}ms, "
            f"unreadable={result['unreadable']}"
        )
        return result


async def _run_synthesis(
    db: Session,
    tender_id: int,
    per_doc_summaries: list[dict],
    deep_scrape_text: str,
    cost_footer: str,
) -> dict:
    """Synthesize the 7-section report from per-doc summaries (text-only, Sonnet 4.6).

    Does NOT re-send the PDFs — synthesis works off the compact per-doc JSONs.
    Returns the parsed analysis dict in the same shape _persist_analysis_results expects.
    """
    from app.services.ai_service import call_ai

    linked_docs_note = _get_linked_documents_note(db, tender_id, [])

    payload = {
        "tender_id": tender_id,
        "per_document_summaries": per_doc_summaries,
        "deep_scrape_excerpt": (deep_scrape_text or "")[:8000],
        "linked_documents_note": linked_docs_note,
        "cost_footer_placeholder": cost_footer,
    }

    user_prompt = (
        "Below is the JSON payload containing per-document summaries for every document in this tender, "
        "plus deep-scrape text from the portal. Synthesize the comprehensive 10-section forensic markdown "
        "report as specified in the system prompt (sections SECTION 1 through SECTION 10, in order). End "
        "with the cost footer line using the placeholder values from `cost_footer_placeholder`.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)[:80000]}\n```\n\n"
        "Output ONLY the markdown report. Do not append any JSON block, code fence, or commentary after "
        "the cost footer line — the report ends at the cost footer."
    )

    # Dynamic max_tokens — Sonnet 4.6 supports 64K output; the prior hardcoded
    # 16K silently truncated the 7-section report on dense tenders, forcing
    # users to re-prompt for missing sections. Combined with the reliability
    # layer's auto-continuation, this gives the model both room to finish AND
    # a graceful fallback if it overshoots.
    from app.services.langchain.model_limits import (
        compute_dynamic_max_tokens, render_token_budget_protocol,
    )
    _synthesis_model = _settings.tender_analyzer_synthesis_model or "claude-sonnet-4-6"
    _synthesis_input_chars = (
        len(SYNTHESIS_SYSTEM_PROMPT) + len(user_prompt) + 2000  # safety pad
    )
    # Cap at 20K tokens. The 10-section forensic report (markdown + trailing
    # JSON) typically lands at 12-18K tokens; allowing Sonnet 4.6's full 64K
    # ceiling here pushes generation past the 300s HTTP read timeout and the
    # call dies mid-stream, leaving the session in a bad state. 20K gives
    # ~25% headroom over the empirical max while keeping gen time bounded.
    _synthesis_max_tokens = compute_dynamic_max_tokens(
        _synthesis_model,
        input_chars=_synthesis_input_chars,
        requested=20_000,
    )

    # Append Token Budget Protocol so the model self-prioritizes if budget
    # gets tight. Order reflects bid-decision criticality: eligibility first
    # (a NO-GO renders everything else moot), then deadlines and rejection
    # risks (skipping these is how bids get summarily disqualified), then
    # the rest of the report.
    _budget_block = render_token_budget_protocol(
        max_tokens=_synthesis_max_tokens,
        priority_hierarchy=(
            "1. SECTION 3 — Eligibility GO/NO-GO table + verdict (decision-defining)\n"
            "2. SECTION 1 — Tender summary, Basic Identification, Commercials\n"
            "3. SECTION 5 — Negative keywords & rejection risks\n"
            "4. SECTION 10 — Prioritized action plan\n"
            "5. SECTION 7 — Costing basis handoff (7.1 - 7.10)\n"
            "6. SECTION 8 — What's missing / unreadable docs\n"
            "7. SECTION 2 — Complete requirements extraction (7 categories)\n"
            "8. SECTION 6 — Required documents list\n"
            "9. SECTION 9 — Key risks & cross-document conflicts\n"
            "10. SECTION 4 — Scoring-based evaluation (skip block if pure L1)"
        ),
    )

    response_text = await _with_overload_retry(
        lambda: call_ai(
            system_prompt=SYNTHESIS_SYSTEM_PROMPT + "\n\n" + _budget_block,
            user_prompt=user_prompt,
            db=db,
            agent_name="tender_synthesis",
            model_override=_synthesis_model,
            max_tokens_override=_synthesis_max_tokens,
            # Synthesis aggregates structured per-doc summaries into a fixed
            # 10-section report — low temperature keeps section ordering, table
            # contents and the trailing JSON schema stable across re-runs.
            temperature_override=0.2,
        ),
        label=f"Synthesis tender {tender_id}",
    )

    # Synthesis produces markdown only now (no trailing JSON tail). Older
    # in-flight prompt-cache hits may still emit a fenced JSON tail — strip
    # it defensively so the artifact viewer never shows raw JSON.
    markdown_only = _strip_trailing_json_fence(response_text)
    return {"report_markdown": markdown_only}


def _build_fallback_report_from_per_doc(
    *,
    tender_id: int,
    per_doc_summaries: list[dict],
    cost_footer: str,
    unreadable_count: int,
    total: int,
) -> str:
    """Build a minimum-viable 10-section report when the synthesis LLM call
    returned empty markdown.

    Used by ``_run_v2_analysis`` as a deterministic fallback so
    ``TenderAnalysisSummary.requirement_summary`` is never empty when at
    least one per-doc extraction succeeded. Downstream consumers (the
    costing auto-chain, the analysis artifact panel) need SOME structured
    tender context; an empty summary forces them to fall back to raw PDF
    extraction which often overflows context.

    The output is markdown shaped like the canonical 10-section forensic
    report so the existing renderers and parsers don't need to special-case
    it. Contents are mechanical aggregation — no LLM call. Sections that
    require synthesis (eligibility GO/NO-GO judgment, scoring projection,
    costing handoff, regulatory cross-cuts) get placeholder text noting
    the synthesis fell through.
    """
    if not per_doc_summaries:
        return (
            f"# Tender Analysis Report — Fallback (Tender #{tender_id})\n\n"
            f"_Synthesis pass produced no narrative AND no per-document "
            f"extractions succeeded ({total - unreadable_count}/{total} "
            f"readable). The tender may be image-only, password-protected, "
            f"or in a format the analyzer couldn't process. Please re-upload "
            f"as text-searchable PDFs._\n\n"
            f"{cost_footer}\n"
        )

    lines: list[str] = []
    lines.append(f"# Tender Analysis Report — Tender #{tender_id}")
    lines.append("")
    lines.append(
        f"_Synthesis pass returned no narrative; this report is a deterministic "
        f"aggregation of the per-document extraction layer "
        f"({len(per_doc_summaries)}/{total} documents readable). For a "
        f"comprehensive synthesised report, retry the analysis._"
    )
    lines.append("")

    # SECTION 1 — Tender summary from key_facts + commercials of every per-doc
    lines.append("### SECTION 1: TENDER SUMMARY & KEY INTELLIGENCE")
    lines.append("")
    facts_by_doc: list[tuple[str, dict]] = []
    for s in per_doc_summaries:
        kf = (s.get("key_facts") or {})
        if isinstance(kf, dict) and kf:
            facts_by_doc.append((s.get("doc_name", "(unknown)"), kf))
    if facts_by_doc:
        lines.append("**Basic Identification (from extracted key_facts):**")
        for doc_name, kf in facts_by_doc:
            lines.append(f"- From `{doc_name}`:")
            for k in ("tender_reference", "issuing_authority", "scope_summary",
                      "estimated_value", "currency"):
                v = kf.get(k)
                if v:
                    lines.append(f"  - {k.replace('_', ' ').title()}: {v}")
        lines.append("")
    else:
        lines.append("_No structured key_facts extracted. See per-doc detail below._")
        lines.append("")

    # Commercials block — surface the verbatim per-doc commercials so the
    # fallback report still renders the Section 1 Commercials table.
    commercial_fields = (
        ("tender_no", "Tender No."),
        ("name_of_work", "Name of Work"),
        ("signing_officer", "Signing Officer"),
        ("portal", "Portal"),
        ("advertised_value", "Advertised Value"),
        ("emd", "EMD"),
        ("tender_doc_cost", "Tender Doc Cost"),
        ("closing_date_raw", "Closing Date"),
        ("bid_validity", "Bid Validity"),
        ("contract_duration", "Contract Duration"),
        ("evaluation_method", "Evaluation Method"),
        ("min_technical_score", "Min Technical Score"),
        ("jv_allowed", "JV Allowed"),
    )
    commercial_rows: list[tuple[str, str, str]] = []
    for s in per_doc_summaries:
        c = s.get("commercials") or {}
        if not isinstance(c, dict):
            continue
        for key, label in commercial_fields:
            v = c.get(key)
            if v:
                commercial_rows.append(
                    (label, str(v), s.get("doc_name", "?"))
                )
    if commercial_rows:
        lines.append("**Commercials (verbatim from per-doc extraction):**")
        lines.append("")
        lines.append("| Field | Value | Source Document |")
        lines.append("|---|---|---|")
        seen: set[tuple[str, str]] = set()
        for label, value, doc in commercial_rows:
            key = (label, value)
            if key in seen:
                continue
            seen.add(key)
            value_cell = value.replace("|", "\\|")[:200]
            lines.append(f"| {label} | {value_cell} | {doc} |")
        lines.append("")

    # Aggregate dates + amounts
    all_dates: list[dict] = []
    all_amounts: list[dict] = []
    for s in per_doc_summaries:
        all_dates.extend(s.get("dates") or [])
        all_amounts.extend(s.get("amounts") or [])
    if all_dates:
        lines.append("**Key dates identified:**")
        for d in all_dates[:15]:
            lbl = d.get("label", "")
            raw = d.get("raw") or d.get("iso_date") or ""
            lines.append(f"- {lbl}: {raw}")
        lines.append("")
    if all_amounts:
        lines.append("**Key amounts identified:**")
        for a in all_amounts[:15]:
            lbl = a.get("label", "")
            amt = a.get("amount", "")
            ccy = a.get("currency") or ""
            lines.append(f"- {lbl}: {amt} {ccy}".strip())
        lines.append("")

    lines.append(
        "**DRPL Recommendation:** `CONDITIONAL — synthesis pass did not run; "
        "verdict requires manual review against DRPL company data.`"
    )
    lines.append("")

    # SECTION 2 — Document requirements (aggregated)
    lines.append("### SECTION 2: COMPLETE REQUIREMENTS EXTRACTION")
    lines.append("")
    all_reqs: list[dict] = []
    for s in per_doc_summaries:
        for r in (s.get("requirements") or []):
            r2 = dict(r)
            r2.setdefault("document_name", s.get("doc_name", "?"))
            all_reqs.append(r2)
    if all_reqs:
        for r in all_reqs[:50]:
            cat = r.get("category", "?")
            text = r.get("text", "")[:300]
            doc = r.get("document_name", "?")
            lines.append(f"- **[{cat}]** {text} _(from {doc})_")
        if len(all_reqs) > 50:
            lines.append(f"- _… plus {len(all_reqs) - 50} more requirements (see per-doc detail)_")
    else:
        lines.append("_No structured requirements extracted from per-doc passes._")
    lines.append("")

    # SECTION 3 — Eligibility flags
    lines.append("### SECTION 3: ELIGIBILITY GO/NO-GO ASSESSMENT (EVIDENCE-BASED)")
    lines.append("")
    elig: list[dict] = []
    for s in per_doc_summaries:
        for f in (s.get("eligibility_flags") or []):
            elig.append({**f, "document_name": s.get("doc_name", "?")})
    if elig:
        lines.append("**Eligibility criteria extracted:**")
        for e in elig[:30]:
            crit = e.get("criterion", "")
            val = e.get("value", "")
            mand = "**MANDATORY**" if e.get("mandatory") else "_desirable_"
            doc = e.get("document_name", "?")
            lines.append(f"- {crit}: {val} ({mand}, from {doc})")
        lines.append("")
        lines.append(
            "_GO/NO-GO judgment requires manual review since the synthesis "
            "pass did not run. The criteria above are extracted from the "
            "per-doc layer but have not been cross-referenced against DRPL's "
            "capabilities._"
        )
    else:
        lines.append("_No structured eligibility flags extracted._")
    lines.append("")

    # SECTION 4 — Scoring (placeholder; requires synthesis to project DRPL position)
    lines.append("### SECTION 4: SCORING-BASED EVALUATION ANALYSIS")
    lines.append("")
    lines.append(
        "_Synthesis pass did not run — scoring-framework analysis is not available. "
        "If the tender uses QCBS or marks-based evaluation, re-run the analysis once "
        "any unreadable documents are recovered._"
    )
    lines.append("")

    # SECTION 5 — Critical clauses / negative keywords
    lines.append("### SECTION 5: NEGATIVE KEYWORDS & REJECTION RISKS")
    lines.append("")
    crits: list[dict] = []
    for s in per_doc_summaries:
        for c in (s.get("critical_clauses") or []):
            crits.append({**c, "document_name": s.get("doc_name", "?")})
    if crits:
        lines.append("| # | Quoted Clause | Flag Type | Severity | Page | Source Document |")
        lines.append("|---|---|---|---|---|---|")
        for i, c in enumerate(crits[:30], 1):
            text = (c.get("text") or "").replace("|", "\\|")[:200]
            ftype = c.get("flag_type", "?")
            sev = c.get("severity", "?")
            page = c.get("page") or "?"
            doc = c.get("document_name", "?")
            lines.append(f"| {i} | {text} | {ftype} | {sev} | {page} | {doc} |")
        lines.append("")
    else:
        lines.append("_No critical clauses flagged in per-doc extraction._")
        lines.append("")

    # SECTION 6 — Required documents (placeholder; deterministic aggregation
    # of mandatory items from per-doc requirements would duplicate Section 2)
    lines.append("### SECTION 6: REQUIRED DOCUMENTS LIST")
    lines.append("")
    lines.append(
        "_Synthesis pass did not run — required-documents reconciliation across "
        "per-doc summaries is unavailable. See Section 2 for raw per-doc requirements; "
        "items in the `eligibility`, `terms_conditions`, and `compliance` categories "
        "usually correspond to documents the bidder must submit._"
    )
    lines.append("")

    # SECTION 7 — Costing basis handoff (placeholder)
    lines.append("### SECTION 7: COSTING BASIS HANDOFF (FOR COSTING AGENT)")
    lines.append("")
    lines.append(
        "_Synthesis pass did not run — structured costing handoff (pricing format, "
        "BOQ structure, tax treatment, escalation, payment milestones, LD, PBG) "
        "is not available. Refer to per-doc summaries in Sections 2 and 5 for raw "
        "financial / terms_conditions / penalty clauses._"
    )
    lines.append("")

    # SECTION 8 — Missing items
    lines.append("### SECTION 8: WHAT'S MISSING")
    lines.append("")
    missing_seen: set[str] = set()
    for s in per_doc_summaries:
        for m in (s.get("missing_info_flags") or []):
            if isinstance(m, str) and m not in missing_seen:
                missing_seen.add(m)
                lines.append(f"- {m}")
    if unreadable_count > 0:
        lines.append(
            f"- {unreadable_count}/{total} document(s) were unreadable. "
            f"Re-upload as text-searchable PDFs to recover their content."
        )
    if not missing_seen and unreadable_count == 0:
        lines.append("_No missing-info flags raised by per-doc extraction._")
    lines.append("")

    # SECTION 9 — Key risks & cross-document conflicts (placeholder)
    lines.append("### SECTION 9: KEY RISKS & OBSERVATIONS")
    lines.append("")
    lines.append(
        "_Synthesis pass did not run — cross-document conflict detection and "
        "regulatory cross-cuts are unavailable. Refer to the per-doc requirements "
        "in Section 2 and critical clauses in Section 5._"
    )
    lines.append("")
    lines.append("### SECTION 10: NEXT STEPS — PRIORITIZED ACTION PLAN")
    lines.append("")
    lines.append("- **Immediate**: Verify all per-doc requirements above are accurate.")
    if unreadable_count > 0:
        lines.append(
            f"- **Immediate**: Re-upload {unreadable_count} unreadable "
            f"document(s) and retry analysis to get a synthesised report."
        )
    lines.append(
        "- **Before bid submission**: Run a full re-analysis once any "
        "missing/unreadable documents are uploaded."
    )
    lines.append("")
    lines.append(cost_footer)
    lines.append("")
    return "\n".join(lines)


def _strip_trailing_json_fence(response_text: str) -> str:
    """Strip a trailing ```json ... ``` fence from the synthesis response.

    The current synthesis prompt instructs the model to emit markdown only,
    but prompt-cache hits from older prompt versions may still append a
    JSON tail. Drop it so the artifact viewer never shows raw JSON.
    """
    if not response_text:
        return ""
    fence_idx = response_text.rfind("```json")
    if fence_idx == -1:
        return response_text.strip()
    close_idx = response_text.find("```", fence_idx + len("```json"))
    if close_idx == -1:
        return response_text.strip()
    return response_text[:fence_idx].rstrip()


def _persist_per_doc_results(
    db: Session, tender_id: int, per_doc_results: list[dict],
) -> tuple[list[dict], int]:
    """Write one per_doc_summary row per document and return the summaries
    the synthesis reads, plus how many documents were unreadable.

    Per-row commits so a partial batch survives a downstream failure --
    this is what lets the Costing Researcher hydrate scope from per-doc
    summaries even when synthesis crashes after the loop. A summary reused
    from an earlier read already has its row and is not written again.
    Shared by the tender analyzer and the chat-upload analysis.
    """
    from app.models.document_analysis import DocumentExtractionResult

    successful_summaries: list[dict] = []
    unreadable_count = 0
    for r in per_doc_results:
        if r.get("unreadable"):
            unreadable_count += 1
            successful_summaries.append({
                "doc_id": r["doc_id"],
                "doc_name": r["doc_name"],
                "unreadable": True,
                "error": r.get("error", "unknown"),
            })
            try:
                db.add(DocumentExtractionResult(
                    tender_id=tender_id,
                    document_id=r["doc_id"],
                    document_name=r["doc_name"],
                    extraction_type="per_doc_summary",
                    items=[],
                    extraction_model=_settings.tender_analyzer_per_doc_model,
                    summary_json={
                        "unreadable": True,
                        "error": r.get("error"),
                        "doc_name": r["doc_name"],
                        **({SOURCE_KEY: source_tag(
                            sha1=r["source_sha1"],
                            model=_settings.tender_analyzer_per_doc_model,
                            prompt=PER_DOC_SYSTEM_PROMPT,
                            page_count=r.get("page_count", 0),
                        )} if r.get("source_sha1") else {}),
                    },
                ))
                db.commit()
            except Exception as _persist_err:
                logger.warning(
                    f"[v2] failed to persist unreadable marker for doc "
                    f"{r['doc_id']}: {_persist_err}"
                )
                try:
                    db.rollback()
                except Exception:
                    pass
            continue

        summary = r.get("summary") or {}
        # The provenance tag stays in the stored row; the synthesis prompt
        # gets the summary exactly as it always did.
        successful_summaries.append(strip_source(summary))
        if r.get("cached"):
            # The row this summary came from is already in the table.
            continue
        try:
            db.add(DocumentExtractionResult(
                tender_id=tender_id,
                document_id=r["doc_id"],
                document_name=r["doc_name"],
                extraction_type="per_doc_summary",
                items=summary.get("requirements", []),
                extraction_model=_settings.tender_analyzer_per_doc_model,
                summary_json=summary,
            ))
            db.commit()
        except Exception as _persist_err:
            logger.warning(
                f"[v2] failed to persist per-doc summary for doc "
                f"{r['doc_id']}: {_persist_err}"
            )
            try:
                db.rollback()
            except Exception:
                pass

    return successful_summaries, unreadable_count


async def _run_v2_analysis(db: Session, tender_id: int, force_refresh: bool = False) -> dict:
    """v2 orchestrator: bounded-parallel per-doc vision extraction + single synthesis pass.

    Returns the same dict shape as the v1 paths so _persist_analysis_results and the
    pipeline graph downstream continue to work unchanged.
    """
    from app.models.tender import TenderDocument, Tender
    from app.models.document_analysis import DocumentExtractionResult
    from app.services.langchain.provider_config import estimate_cost
    from app.services.ai_service import _get_pdf_page_count

    documents = db.query(TenderDocument).filter(
        TenderDocument.tender_id == tender_id,
    ).all()

    # file_path is a StorageService key, not a local path. Existence is
    # validated per-coroutine inside _analyze_single_document_native via
    # as_local_file; leaked exceptions are normalized to the "unreadable"
    # shape by the gather(return_exceptions=True) below.
    pdf_docs = [
        d for d in documents
        if d.file_path and d.file_path.lower().endswith(".pdf")
    ]

    # On-demand fallback: no NIT PDF captured but the tender has a public
    # document link → fetch it from the link, then re-query. Flag-gated,
    # never raises. See 2026-07-21-backend-nit-link-fetch-design.
    if not pdf_docs and _settings.nit_link_fetch_enabled:
        tender_row = db.query(Tender).filter(Tender.id == tender_id).first()
        has_link = bool(
            (getattr(tender_row, "nit_document_links", None) or [])
            or (getattr(tender_row, "document_links", None) or [])
        ) if tender_row else False
        if has_link:
            from app.services.nit_link_fetch_service import fetch_document_links_for_tender
            fr = fetch_document_links_for_tender(db, tender_id)
            logger.info(f"[v2] tender {tender_id}: on-demand NIT link fetch → "
                        f"fetched={fr.fetched} skipped_not_public={fr.skipped_not_public} "
                        f"skipped_dedup={fr.skipped_dedup} failed={fr.failed}")
            if fr.fetched:
                documents = db.query(TenderDocument).filter(
                    TenderDocument.tender_id == tender_id,
                ).all()
                pdf_docs = [
                    d for d in documents
                    if d.file_path and d.file_path.lower().endswith(".pdf")
                ]

    if not pdf_docs:
        logger.info(f"[v2] Tender {tender_id} has no PDF documents — skipping v2")
        return {
            "tender_id": tender_id,
            "analysis": {"error": "no_pdf_documents", "summary": "No PDF documents available."},
            "metrics": {"method": "v2_native_parallel", "documents_count": 0},
        }

    parallel_limit = max(1, _settings.tender_analyzer_max_parallel)
    sem = asyncio.Semaphore(parallel_limit)
    total = len(pdf_docs)

    _mode_label = "SEQUENTIAL" if parallel_limit == 1 else f"PARALLEL×{parallel_limit}"
    logger.info(
        f"[v2] Starting {_mode_label} analysis of {total} PDFs for tender {tender_id} "
        f"(model={_settings.tender_analyzer_per_doc_model})"
    )
    print(
        f"[DRPL ANALYZER] tender {tender_id}: starting {_mode_label} per-doc "
        f"extraction over {total} PDFs",
        flush=True,
    )

    # Emit initial progress so the frontend can render a live status pill
    # ("Reading 0 of 5 documents…") immediately and keep the SSE channel hot.
    from app.services.ai_service import _emit_reliability_event
    _emit_reliability_event(None, "analyzer_progress", {
        "phase": "starting",
        "completed": 0,
        "total": total,
    })

    t_pipeline = time.monotonic()

    # Wrap each per-doc task so we can emit a progress event as soon as it
    # finishes (regardless of original index order). A shared counter avoids
    # racy ordering — we just report "k of N done" cumulatively.
    _completed = [0]
    _completed_lock = asyncio.Lock()

    async def _run_with_progress(idx: int, doc) -> dict:
        result = await _analyze_single_document_native(
            tender_id=tender_id,
            doc_id=doc.id,
            doc_name=doc.file_name or os.path.basename(doc.file_path),
            doc_path=doc.file_path,
            doc_index=idx + 1,
            doc_total=total,
            semaphore=sem,
            force_refresh=force_refresh,
        )
        async with _completed_lock:
            _completed[0] += 1
            done = _completed[0]
        _emit_reliability_event(None, "analyzer_progress", {
            "phase": "per_doc_complete",
            "completed": done,
            "total": total,
            "doc_name": doc.file_name or os.path.basename(doc.file_path),
        })
        return result

    # Release the orchestrator's DB connection before the long gather —
    # without this, the outer db session holds one TCP socket idle through
    # 5-15 min of Anthropic vision calls and the OS / Neon kill it. See
    # _release_db_before_long_await for the failure mode this guards.
    _release_db_before_long_await(db, label="per_doc_gather")

    raw_results = await asyncio.gather(
        *[_run_with_progress(idx, doc) for idx, doc in enumerate(pdf_docs)],
        return_exceptions=True,
    )

    # Defense in depth: convert any exception leaks into the standard "unreadable" shape
    per_doc_results: list[dict] = []
    for idx, r in enumerate(raw_results):
        if isinstance(r, BaseException):
            doc = pdf_docs[idx]
            logger.error(f"[v2] Unhandled exception for doc {doc.id}: {r}")
            per_doc_results.append({
                "doc_id": doc.id,
                "doc_name": doc.file_name or os.path.basename(doc.file_path),
                "page_count": 0,
                "summary": None,
                "unreadable": True,
                "error": f"unhandled_exception: {type(r).__name__}: {str(r)[:200]}",
                "elapsed_ms": 0,
            })
        else:
            per_doc_results.append(r)

    # Persist per-doc summary_json to DocumentExtractionResult (one row per doc).
    successful_summaries, unreadable_count = _persist_per_doc_results(
        db, tender_id, per_doc_results,
    )

    # Pull deep-scrape text from the tender record so synthesis sees portal-side data too
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    deep_scrape_parts = []
    if tender:
        for label, val in (
            ("Full Description", tender.full_description),
            ("Eligibility Criteria", tender.eligibility_criteria),
            ("Technical Specifications", tender.technical_specifications),
            ("Evaluation Criteria", tender.evaluation_criteria),
        ):
            if val:
                deep_scrape_parts.append(f"--- {label} ---\n{val}")
    deep_scrape_text = "\n\n".join(deep_scrape_parts)

    # Cost tracking — pulled from APIUsageLog rows that the per-doc + synthesis calls already wrote
    # (see callback_handler / _log_usage in ai_service). We compute a footer placeholder string here
    # and re-inject the real numbers after synthesis.
    cost_footer_placeholder = "_Cost: $COST  |  Tokens: TIN input / TOUT output  |  Documents analyzed: NREAD (of NTOTAL)._"

    # Signal that per-doc extraction is done and synthesis is starting.
    _emit_reliability_event(None, "analyzer_progress", {
        "phase": "synthesis_start",
        "completed": total,
        "total": total,
    })

    # Synthesis is another 1-6 min Sonnet call — release the DB connection
    # before awaiting it so the cost aggregation + TenderAnalysisSummary
    # writes after this block get a fresh checkout (validated by pool_pre_ping).
    _release_db_before_long_await(db, label="synthesis_call")

    # Synthesis pass (text-only Sonnet)
    try:
        analysis = await _run_synthesis(
            db=db,
            tender_id=tender_id,
            per_doc_summaries=successful_summaries,
            deep_scrape_text=deep_scrape_text,
            cost_footer=cost_footer_placeholder,
        )
    except Exception as e:
        logger.error(f"[v2] Synthesis failed for tender {tender_id}: {e}")
        print(
            f"[DRPL ANALYZER] tender {tender_id}: synthesis raised "
            f"{type(e).__name__}: {str(e)[:300]}",
            flush=True,
        )
        # A mid-stream ReadTimeout (or any in-flight LLM error) leaves the
        # shared SQLAlchemy session with an invalid transaction — every
        # downstream query then raises PendingRollbackError. Roll back so
        # the empty-markdown fallback + _aggregate_v2_cost + persistence
        # block below all see a usable session.
        try:
            db.rollback()
        except Exception as _rb_err:
            logger.warning(
                f"[v2] rollback after synthesis failure also raised: {_rb_err}"
            )
        # Degrade gracefully — return per-doc data without the synthesised report
        analysis = {
            # Marks this run as degraded so the persist step does NOT overwrite a
            # prior good full-synthesis summary with this fallback.
            "_degraded": True,
            "requirements": {},
            "negative_keywords": [],
            "required_documents": [],
            "missing_items": [
                f"Synthesis pass failed: {str(e)[:200]}. Per-document summaries are still available.",
            ],
            "summary": (
                f"Synthesis failed but per-doc extraction succeeded for "
                f"{total - unreadable_count}/{total} documents."
            ),
            "key_risks": [],
            "action_items": {},
        }

    # _run_synthesis opens its own internal transaction (linked-docs note +
    # call_ai config reads) BEFORE its long Sonnet await, so the db connection
    # held by that transaction is also vulnerable. Release once more here so
    # the cost-aggregation + persist phase below gets a freshly checked-out
    # connection.
    _release_db_before_long_await(db, label="post_synthesis")

    # ── Empty-synthesis fallback ────────────────────────────────────────
    # If synthesis returned without raising but produced no usable
    # `report_markdown` (LLM returned empty content; rare but real on
    # large multi-PDF tenders where input compaction is needed), build
    # a minimum-viable summary from the per-doc rows we already have.
    # This guarantees `requirement_summary` is non-empty downstream so
    # the auto-chain costing flow doesn't fall back to "raw scope only".
    if not (analysis.get("report_markdown") or "").strip():
        print(
            f"[DRPL ANALYZER] tender {tender_id}: synthesis returned "
            f"EMPTY markdown despite {len(successful_summaries)} successful "
            f"per-doc summaries. Falling back to assembled per-doc summary.",
            flush=True,
        )
        logger.warning(
            f"[v2] tender {tender_id}: synthesis returned empty markdown — "
            f"building fallback from {len(successful_summaries)} per-doc summaries"
        )
        # Empty synthesis is also a degraded result — don't let its fallback
        # report clobber a previously-good full analysis.
        analysis["_degraded"] = True
        analysis["report_markdown"] = _build_fallback_report_from_per_doc(
            tender_id=tender_id,
            per_doc_summaries=successful_summaries,
            cost_footer=cost_footer_placeholder,
            unreadable_count=unreadable_count,
            total=total,
        )
        # Set summary too so any consumer reading just `summary` (not
        # `report_markdown`) also sees content.
        if not (analysis.get("summary") or "").strip():
            analysis["summary"] = (
                f"Synthesis pass produced no narrative for tender #{tender_id}, "
                f"but {len(successful_summaries)}/{total} document(s) were "
                f"successfully extracted. The fallback report below "
                f"aggregates the per-document summaries directly."
            )

    # Total cost from APIUsageLog (the per-doc + synthesis calls have already written rows).
    # Must not be fatal: synthesis already succeeded, so a failure here (e.g. a stray
    # PendingRollbackError from an earlier transient session glitch) would otherwise
    # discard the entire 10-section report and force the orchestrator down to the
    # degraded ReAct fallback. Use zeros if aggregation fails; the placeholder
    # `$COST/TIN/TOUT` strings just remain in the markdown footer.
    try:
        cost_summary = _aggregate_v2_cost(db, tender_id, t_pipeline)
    except Exception as _cost_err:
        logger.warning(
            f"[v2] cost aggregation failed for tender {tender_id}: {_cost_err}; "
            f"continuing with zero-cost summary."
        )
        try:
            db.rollback()
        except Exception:
            pass
        cost_summary = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_usd": 0.0,
        }

    # Replace the placeholder in the markdown report with real numbers
    if "report_markdown" in analysis and isinstance(analysis["report_markdown"], str):
        analysis["report_markdown"] = (
            analysis["report_markdown"]
            .replace("$COST", f"${cost_summary['total_cost_usd']:.4f}")
            .replace("TIN", str(cost_summary["total_input_tokens"]))
            .replace("TOUT", str(cost_summary["total_output_tokens"]))
            .replace("NREAD", str(total - unreadable_count))
            .replace("NTOTAL", str(total))
        )

    metrics = {
        "method": "v2_native_parallel",
        "documents_count": total,
        "documents_unreadable": unreadable_count,
        "total_pages": sum(r.get("page_count", 0) for r in per_doc_results),
        "wall_clock_ms": int((time.monotonic() - t_pipeline) * 1000),
        **cost_summary,
    }

    _persist_analysis_results(db, tender_id, analysis, metrics)

    # Persist requirement_summary + cost columns to TenderAnalysisSummary.
    #
    # IMPORTANT: this block uses a get-or-create pattern and sets
    # requirement_summary so that _run_v2_analysis is self-contained
    # regardless of how it is called:
    #
    #   • Via analyze_all_documents (tender_analysis_service.py) — the UI
    #     "Analyse Documents" button. That function also sets requirement_summary
    #     after we return, which becomes a harmless idempotent overwrite.
    #
    #   • Via the auto-chain (_ensure_tender_analysis → run_document_analysis
    #     → _run_v2_analysis). This path bypasses analyze_all_documents entirely,
    #     so WITHOUT this block, requirement_summary was never written and
    #     _ensure_tender_analysis's re-query always returned an empty field —
    #     causing the costing agent to fall back to raw PDF extraction on every
    #     fresh tender even after a successful analysis.
    try:
        from app.models.document_analysis import TenderAnalysisSummary as _TAS
        from datetime import datetime as _dt, timezone as _tz
        _summary_row = db.query(_TAS).filter(
            _TAS.tender_id == tender_id,
        ).first()
        if not _summary_row:
            _summary_row = _TAS(tender_id=tender_id)
            db.add(_summary_row)
        # Non-destructive write: a DEGRADED re-run (synthesis failed or empty,
        # falling back to per-doc aggregation) must NOT wipe a previously-good
        # full analysis. Only overwrite requirement_summary when this run
        # synthesized successfully, or when there is no prior summary to keep.
        _new_report = analysis.get("report_markdown", "") or ""
        _prior_summary = (_summary_row.requirement_summary or "")
        _is_degraded = bool(analysis.get("_degraded"))
        if _is_degraded and _prior_summary.strip():
            logger.warning(
                f"[v2] tender {tender_id}: synthesis degraded — preserving prior "
                f"requirement_summary ({len(_prior_summary)} chars) instead of "
                f"overwriting with the {len(_new_report)}-char fallback"
            )
            # Keep the prior good summary; record the failure on the row below.
        else:
            _summary_row.requirement_summary = _new_report
        _summary_row.analysis_status = "completed"
        _summary_row.analysis_version = "v2"
        _summary_row.last_analyzed_at = _dt.now(_tz.utc)
        _summary_row.documents_analyzed = total - unreadable_count
        _summary_row.total_input_tokens = cost_summary["total_input_tokens"]
        _summary_row.total_output_tokens = cost_summary["total_output_tokens"]
        _summary_row.total_cost_usd = cost_summary["total_cost_usd"]
        _summary_row.per_doc_unreadable_count = unreadable_count
        db.commit()
        _rs_len = len(_summary_row.requirement_summary or "")
        logger.info(
            f"[v2] TenderAnalysisSummary saved for tender {tender_id}: "
            f"requirement_summary={_rs_len} chars, status=completed"
        )
        print(
            f"[DRPL ANALYZER] tender {tender_id}: requirement_summary persisted "
            f"({_rs_len} chars) — auto-chain will find non-empty summary",
            flush=True,
        )

        # Post-analysis enrichment: runs for ALL tenders (not just
        # auto-created ones). Writes title/description using longer-wins
        # semantics (never shortens/clobbers a good scraped value), fills
        # numeric commercials only when empty, and rewrites portal/tender_id
        # and the linked ProposalSession.title only for auto-created rows.
        # Idempotent from the extracted tender intelligence (per-doc
        # key_facts + synthesis markdown).
        try:
            from app.services.tender_enrichment_service import (
                enrich_tender_from_analysis,
            )
            enriched = enrich_tender_from_analysis(db, tender_id)
            if enriched:
                _emit_reliability_event(None, "tender_enriched", {
                    "tender_id": tender_id,
                    **enriched,
                })
        except Exception as _enrich_err:
            logger.warning(
                f"[v2] tender enrichment failed for tender {tender_id}: "
                f"{type(_enrich_err).__name__}: {_enrich_err}",
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass

        # NIT bidding-schedule capture. Runs after the per-doc Haiku pass
        # has populated DocumentExtractionResult.summary_json.doc_type so the
        # parser can skip the documents that positively carry no priced lines
        # (drawings, T&C) — everything else is read, annexures included; see
        # `_pick_nit_source_docs`. The costing agent reads from BOQItem
        # downstream, so this must complete before any costing run.
        #
        # `force=True` makes this the repair path: a tender captured under
        # the old NIT-class allowlist, which dropped its annexures, gets a
        # complete schedule the next time its analysis is re-run.
        # Plan: ~/.claude/plans/now-i-need-to-synchronous-taco.md
        #
        # It goes through `recapture_schedule_for_analysis`, which takes the
        # same per-tender lock `ensure_boq_parsed` uses and skips the
        # re-capture while a costing is running on the tender: a forced
        # re-parse replaces every BOQItem id, and a costing merging its
        # batches against the old ids would keep the stale ones.
        try:
            from app.services.boq_parser_service import recapture_schedule_for_analysis
            _readable = [r for r in per_doc_results if not r.get("unreadable")]
            _all_cached = bool(_readable) and all(r.get("cached") for r in _readable)
            if _all_cached and not force_refresh and _schedule_is_current(db, tender_id):
                # Every document is byte-identical to the analysis that
                # captured this schedule, and nothing arrived since: the
                # forced re-read would only replace the rows with themselves.
                from app.services.boq_parser_service import parse_boq_from_tender
                _boq_rows = await parse_boq_from_tender(db, tender_id, force=False)
                logger.info(
                    f"[v2] tender {tender_id}: bidding schedule kept "
                    f"({len(_boq_rows)} rows) — documents unchanged since it was captured"
                )
            else:
                _boq_rows = await recapture_schedule_for_analysis(db, tender_id)
            logger.info(
                f"[v2] tender {tender_id}: bidding schedule captured "
                f"({len(_boq_rows)} BOQItem rows)"
            )
        except Exception as _boq_err:
            logger.warning(
                f"[v2] NIT schedule extraction failed for tender {tender_id}: "
                f"{type(_boq_err).__name__}: {_boq_err}",
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[v2] Could not write to TenderAnalysisSummary: {e}", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass

    # Final progress signal — synthesis done, persistence done, ready to render.
    _emit_reliability_event(None, "analyzer_progress", {
        "phase": "complete",
        "completed": total,
        "total": total,
    })

    logger.info(
        f"[v2] Tender {tender_id} done: {total - unreadable_count}/{total} docs, "
        f"{metrics['wall_clock_ms']}ms wall, ${cost_summary['total_cost_usd']:.4f}"
    )

    return {
        "tender_id": tender_id,
        "analysis": analysis,
        "metrics": metrics,
    }


def _schedule_is_current(db: Session, tender_id: int) -> bool:
    """Captured BOQ rows exist and no schedule-bearing document arrived after
    they were captured. False on any doubt, so the forced re-capture runs."""
    try:
        from app.models.costing_template import BOQItem
        from app.services.boq_parser_service import _schedule_predates_latest_upload
        rows = db.query(BOQItem).filter(BOQItem.tender_id == tender_id).all()
        if not rows:
            return False
        return _schedule_predates_latest_upload(db, tender_id, rows) is None
    except Exception as e:
        logger.warning(f"[v2] schedule freshness check failed for tender {tender_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _aggregate_v2_cost(db: Session, tender_id: int, since_monotonic: float) -> dict:
    """Sum input/output tokens and cost from APIUsageLog rows written by this v2 run.

    We bound the lookup by created_at >= the wall-clock equivalent of `since_monotonic`
    to avoid double-counting prior runs. agent_name filter narrows further.
    """
    from datetime import datetime, timedelta, timezone
    from app.models.api_usage import APIUsageLog
    from app.services.langchain.provider_config import estimate_cost

    # Convert monotonic delta to wall clock cutoff (approximate but sufficient)
    seconds_ago = max(0.0, time.monotonic() - since_monotonic)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago + 5)

    rows = db.query(APIUsageLog).filter(
        APIUsageLog.created_at >= cutoff,
        APIUsageLog.agent_name.in_(["tender_per_doc", "tender_synthesis"]),
    ).all()

    total_in = sum(r.tokens_input or 0 for r in rows)
    total_out = sum(r.tokens_output or 0 for r in rows)
    # Recompute cost from current pricing tables (more accurate than per-row sum if pricing changed)
    total_cost = 0.0
    for r in rows:
        total_cost += estimate_cost(
            r.model or "",
            {"input_tokens": r.tokens_input or 0, "output_tokens": r.tokens_output or 0},
        )

    return {
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": round(total_cost, 4),
        "api_call_count": len(rows),
    }


def _get_linked_documents_note(db: Session, tender_id: int, pdf_paths: list[str]) -> str:
    """Build a note about linked/child documents available for this tender."""
    try:
        from app.models.tender import TenderDocument
        parent_docs = db.query(TenderDocument).filter(
            TenderDocument.tender_id == tender_id,
            TenderDocument.parent_document_id.is_(None),
        ).all()

        notes = []
        for parent in parent_docs:
            children = db.query(TenderDocument).filter(
                TenderDocument.parent_document_id == parent.id,
            ).all()
            if children:
                child_names = [c.file_name for c in children]
                notes.append(
                    f"Master document '{parent.file_name}' has {len(children)} linked document(s): "
                    f"{', '.join(child_names)}. Analyze each one."
                )
            elif parent.extraction_status == "processing":
                notes.append(
                    f"Document '{parent.file_name}' is still being processed for linked documents."
                )

        if notes:
            return "LINKED DOCUMENTS:\n" + "\n".join(notes)
    except Exception as e:
        logger.warning(f"Failed to get linked documents note: {e}")
    return ""
