"""
DRPL Backend - Batch Processing Service
Integrates with Claude's Message Batches API for 50% cost discount on bulk processing.

Key features:
- Submit batches of tender analysis requests (classify, relevance, risk, summary)
- Poll for batch completion
- Process results and update tenders
- Cancel in-progress batches
- Full cost tracking with batch pricing (50% discount)
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tender import Tender
from app.models.message_batch import MessageBatch, MessageBatchItem
from app.models.platform_setting import PlatformSetting
from app.models.api_usage import APIUsageLog

logger = logging.getLogger(__name__)
settings = get_settings()

ANTHROPIC_BATCHES_URL = "https://api.anthropic.com/v1/messages/batches"
ANTHROPIC_API_VERSION = "2023-06-01"


# --- Helpers ---

def _get_api_key(db: Session) -> str:
    """Resolve Anthropic API key: platform setting → .env."""
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "anthropic_api_key").first()
    if setting and setting.value and setting.value.strip() and setting.value != "••••••••":
        return setting.value
    return settings.anthropic_api_key


def _get_model(db: Session) -> str:
    """Resolve model: platform setting → .env."""
    setting = db.query(PlatformSetting).filter(PlatformSetting.key == "ai_model").first()
    if setting and setting.value:
        return setting.value
    return settings.ai_model


def _get_headers(api_key: str) -> dict:
    """Build Anthropic API request headers."""
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }


def _estimate_batch_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate batch cost (50% of standard rates)."""
    pricing = {
        "claude-opus-4-6": {"input": 7.50, "output": 37.50},     # 50% of 15/75
        "claude-sonnet-4-6": {"input": 1.50, "output": 7.50},    # 50% of 3/15
        "claude-opus-4-5-20251101": {"input": 7.50, "output": 37.50},
        "claude-sonnet-4-5-20250929": {"input": 1.50, "output": 7.50},
        "claude-haiku-4-5": {"input": 0.40, "output": 2.00},
        "claude-haiku-4-5-20251001": {"input": 0.40, "output": 2.00},
        "claude-opus-4-1-20250805": {"input": 7.50, "output": 37.50},
        "claude-sonnet-4-20250514": {"input": 1.50, "output": 7.50},
        "claude-opus-4-20250514": {"input": 7.50, "output": 37.50},
        "claude-3-5-sonnet-20241022": {"input": 1.50, "output": 7.50},
        "claude-3-haiku-20240307": {"input": 0.125, "output": 0.625},
        "claude-3-opus-20240229": {"input": 7.50, "output": 37.50},
    }
    rates = pricing.get(model, {"input": 1.50, "output": 7.50})
    return round((input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000, 6)


# --- Agent Prompts (same as ai_service.py) ---

AGENT_PROMPTS = {
    "classifier": {
        "system": """You are an AI analyst for DRPL Manufacturing, a company specializing in
mechanical and electrical engineering for Indian Railways. Classify the given tender into exactly
ONE of these categories: Mechanical, Electrical, Civil, IT/Software, Materials/Supplies, Consulting, Other.
Respond with ONLY the category name, nothing else.""",
    },
    "relevance": {
        "system": """You are a relevance scoring agent for DRPL Manufacturing. DRPL specializes in:
- Mechanical engineering for Indian Railways (locomotive components, bogies, couplings, braking systems)
- Electrical engineering for railways (traction motors, transformers, signaling equipment, power systems)
- Annual Maintenance Contracts (AMC) for railway equipment
- Supply of mechanical and electrical spare parts

Score the tender's relevance to DRPL on a scale of 0.0 to 1.0:
- 0.9-1.0: Perfect match (railway mechanical/electrical core work)
- 0.7-0.8: Strong match (related mechanical/electrical or railway work)
- 0.4-0.6: Moderate match (partially relevant industry or scope)
- 0.1-0.3: Weak match (different domain but some overlap)
- 0.0: No relevance

Respond with ONLY a number between 0.0 and 1.0, nothing else.""",
    },
    "risk": {
        "system": """You are a risk assessment agent for DRPL Manufacturing, evaluating government tenders.
Assess the risk level considering:
- Tight deadlines (closing soon, short execution periods)
- High EMD/security deposit requirements relative to tender value
- Unclear or vague scope of work
- Complex technical requirements that may be hard to fulfill
- Unusually low estimated values suggesting compressed margins
- Multiple corrigenda or amendments suggesting instability

Score risk from 0.0 (very low risk) to 1.0 (very high risk).
Respond with ONLY a number between 0.0 and 1.0, nothing else.""",
    },
    "summary": {
        "system": """You are a tender analyst for DRPL Manufacturing. Write a concise 2-3 sentence
executive summary of the tender. Highlight: what is being procured, key requirements or quantities,
and whether it aligns with DRPL's mechanical/electrical railway capabilities. Be factual and specific.""",
    },
}


def _build_tender_context(tender: Tender) -> str:
    """Build tender context string (same as ai_service.py)."""
    parts = [
        f"Title: {tender.title}",
        f"Portal: {tender.portal}",
        f"Tender ID: {tender.tender_id}",
        f"Department: {tender.department or 'Not specified'}",
        f"Organisation: {tender.organisation or 'Not specified'}",
        f"Description: {tender.description or 'No description available'}",
        f"Estimated Value: {tender.estimated_value or 'Not specified'} {tender.currency or 'INR'}",
        f"EMD Amount: {tender.emd_amount or 'Not specified'}",
        f"Opening Date: {tender.opening_date or 'Not specified'}",
        f"Closing Date: {tender.closing_date or 'Not specified'}",
        f"Status: {tender.status}",
    ]
    return "\n".join(parts)


# --- Core Batch Operations ---

async def create_tender_analysis_batch(
    db: Session,
    batch_size: int = 50,
    agents: list[str] = None,
    user_id: int = None,
) -> MessageBatch:
    """
    Create and submit a batch of tender analysis requests to the Anthropic Batches API.

    Each unanalyzed tender gets 4 requests (classifier, relevance, risk, summary).
    All 4 agents run in a single batch for maximum efficiency.

    Args:
        db: Database session.
        batch_size: Number of tenders to include (max 100,000 requests / 4 agents = 25,000 tenders).
        agents: Which agents to run. Defaults to all 4: ["classifier", "relevance", "risk", "summary"].
        user_id: User who triggered the batch.

    Returns:
        MessageBatch record.
    """
    api_key = _get_api_key(db)
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured. Set it in Platform Settings or .env")

    model = _get_model(db)
    agents = agents or ["classifier", "relevance", "risk", "summary"]

    # Get unanalyzed tenders
    unanalyzed = db.query(Tender).filter(
        Tender.ai_category == None,
    ).limit(batch_size).all()

    if not unanalyzed:
        raise ValueError("No unanalyzed tenders found")

    # Apply redaction if configured
    from app.services.redaction_service import redact_tender_context

    # Build batch requests
    batch_requests = []
    batch_items = []
    tender_ids = []

    for tender in unanalyzed:
        tender_context = redact_tender_context(_build_tender_context(tender), db)
        tender_ids.append(tender.id)

        for agent_name in agents:
            if agent_name not in AGENT_PROMPTS:
                continue

            custom_id = f"tender_{tender.id}_{agent_name}"
            prompt_config = AGENT_PROMPTS[agent_name]

            batch_requests.append({
                "custom_id": custom_id,
                "params": {
                    "model": model,
                    "max_tokens": 1024,
                    "system": prompt_config["system"],
                    "messages": [{"role": "user", "content": tender_context}],
                },
            })

            batch_items.append({
                "custom_id": custom_id,
                "item_type": agent_name,
                "tender_id": tender.id,
            })

    if not batch_requests:
        raise ValueError("No valid batch requests generated")

    # Submit to Anthropic Batches API
    logger.info(f"Submitting batch with {len(batch_requests)} requests for {len(unanalyzed)} tenders")

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            ANTHROPIC_BATCHES_URL,
            headers=_get_headers(api_key),
            json={"requests": batch_requests},
        )

        if response.status_code != 200:
            error_text = response.text
            logger.error(f"Batch creation failed: {response.status_code} - {error_text}")
            raise Exception(f"Batch API error ({response.status_code}): {error_text[:500]}")

        data = response.json()

    # Create MessageBatch record
    batch_record = MessageBatch(
        batch_id=data["id"],
        batch_type="tender_analysis",
        status=data.get("processing_status", "in_progress"),
        model=model,
        total_requests=len(batch_requests),
        processing_count=data.get("request_counts", {}).get("processing", len(batch_requests)),
        succeeded_count=data.get("request_counts", {}).get("succeeded", 0),
        errored_count=data.get("request_counts", {}).get("errored", 0),
        expired_count=data.get("request_counts", {}).get("expired", 0),
        canceled_count=data.get("request_counts", {}).get("canceled", 0),
        results_url=data.get("results_url"),
        metadata_json=json.dumps({
            "tender_ids": tender_ids,
            "agents": agents,
            "tender_count": len(unanalyzed),
        }),
        created_by=user_id,
        expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")) if data.get("expires_at") else None,
    )
    db.add(batch_record)

    # Create individual item records
    for item_info in batch_items:
        item = MessageBatchItem(
            batch_id=data["id"],
            custom_id=item_info["custom_id"],
            item_type=item_info["item_type"],
            tender_id=item_info["tender_id"],
            result_status="pending",
        )
        db.add(item)

    db.commit()
    db.refresh(batch_record)

    logger.info(f"Batch {data['id']} created with {len(batch_requests)} requests")
    return batch_record


# --- Tender relevance SCORING batch (lean, cheap; one request per tender) ---

async def create_tender_scoring_batch(
    db: Session,
    tender_ids: list,
    fanout: dict,
    user_id: Optional[int] = None,
) -> MessageBatch:
    """Submit one relevance-scoring request per representative tender.

    batch_type='tender_scoring'. The cached scoring digest is the system prompt
    (ephemeral cache_control) and `_user_prompt` builds the compact user turn —
    identical to the live reaper path, so batch and live scores are comparable.
    `fanout` ({content_hash: [tender_ids]}) is stored in metadata_json so the
    result processor can copy each representative's score to its duplicates at
    zero extra tokens. Does NOT touch the 4-agent deep-analysis fan-out.
    """
    from app.services.seed_scoring_agent import get_scoring_system_prompt
    from app.services.auto_scoring_service import _user_prompt
    from app.services.auto_scoring_settings import get_scoring_settings

    api_key = _get_api_key(db)
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured. Set it in Platform Settings or .env")

    scoring = get_scoring_settings(db)
    model = scoring["auto_scoring_model"]
    system = get_scoring_system_prompt(db)

    tenders = db.query(Tender).filter(Tender.id.in_(tender_ids)).all()
    if not tenders:
        raise ValueError("No tenders to score")

    batch_requests = []
    batch_items = []
    for t in tenders:
        custom_id = f"score_{t.id}"
        batch_requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": 128,
                "system": [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                "messages": [{"role": "user", "content": _user_prompt(t)}],
            },
        })
        batch_items.append({"custom_id": custom_id, "item_type": "score", "tender_id": t.id})

    logger.info(f"Submitting scoring batch with {len(batch_requests)} requests")

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            ANTHROPIC_BATCHES_URL,
            headers=_get_headers(api_key),
            json={"requests": batch_requests},
        )
        if response.status_code != 200:
            error_text = response.text
            logger.error(f"Scoring batch creation failed: {response.status_code} - {error_text}")
            raise Exception(f"Batch API error ({response.status_code}): {error_text[:500]}")
        data = response.json()

    batch_record = MessageBatch(
        batch_id=data["id"],
        batch_type="tender_scoring",
        status=data.get("processing_status", "in_progress"),
        model=model,
        total_requests=len(batch_requests),
        processing_count=data.get("request_counts", {}).get("processing", len(batch_requests)),
        results_url=data.get("results_url"),
        metadata_json=json.dumps({"fanout": fanout, "tender_ids": list(tender_ids)}),
        created_by=user_id,
        expires_at=datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if data.get("expires_at") else None,
    )
    db.add(batch_record)
    for item_info in batch_items:
        db.add(MessageBatchItem(
            batch_id=data["id"],
            custom_id=item_info["custom_id"],
            item_type=item_info["item_type"],
            tender_id=item_info["tender_id"],
            result_status="pending",
        ))
    db.commit()
    db.refresh(batch_record)

    logger.info(f"Scoring batch {data['id']} created with {len(batch_requests)} requests")
    return batch_record


def apply_scoring_results(db: Session, batch_record: MessageBatch, tender_results: dict) -> int:
    """Write ai_relevance_score/segment from scoring replies, fanning out to duplicates.

    `tender_results` maps representative tender_id -> raw reply text. Every tender
    sharing the representative's content hash (from batch metadata `fanout`) gets
    the same score/segment — no extra tokens for duplicates. Segment respects
    `segment_overridden`; below-threshold flagging matches the live path.
    """
    from app.services.auto_scoring_helpers import (
        parse_score_reply, parse_eligible_reply, compute_segment, is_below_threshold,
    )
    from app.services.auto_scoring_service import _wants_eligibility
    from app.services.auto_scoring_settings import get_scoring_settings

    scoring = get_scoring_settings(db)
    meta = json.loads(batch_record.metadata_json or "{}")
    fanout = meta.get("fanout", {})
    # representative id (first in each group) -> all ids sharing its hash
    rep_to_ids: dict = {}
    for ids in fanout.values():
        if ids:
            rep_to_ids[ids[0]] = ids

    updated = 0
    for rep_id, text in tender_results.items():
        score, reason = parse_score_reply(text)
        eligible = parse_eligible_reply(text)
        target_ids = rep_to_ids.get(rep_id, [rep_id])
        tenders = db.query(Tender).filter(Tender.id.in_(target_ids)).all()
        for t in tenders:
            t.ai_relevance_score = score
            t.fit_reasoning = reason
            # The eligibility line rides on the scoring prompt for IREPS
            # blue-tick tenders; absent means unknown, never not_eligible.
            if eligible is not None and _wants_eligibility(t):
                t.eligibility_status = "eligible" if eligible else "not_eligible"
                t.eligibility_score = score
            if is_below_threshold(t.estimated_value, scoring["value_threshold_inr"]):
                t.below_threshold = True
            if not t.segment_overridden:
                t.segment = compute_segment(
                    t.ai_relevance_score, t.estimated_value,
                    discard_below=scoring["segment_discard_below"],
                    bidable_at=scoring["segment_bidable_at"],
                    value_threshold=scoring["value_threshold_inr"],
                )
            updated += 1
    db.commit()
    return updated


async def poll_batch_status(db: Session, batch_id: str) -> MessageBatch:
    """
    Poll the Anthropic API for batch status updates.

    Args:
        db: Database session.
        batch_id: Anthropic batch ID (msgbatch_xxx).

    Returns:
        Updated MessageBatch record.
    """
    batch_record = db.query(MessageBatch).filter(MessageBatch.batch_id == batch_id).first()
    if not batch_record:
        raise ValueError(f"Batch {batch_id} not found in database")

    api_key = _get_api_key(db)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{ANTHROPIC_BATCHES_URL}/{batch_id}",
            headers=_get_headers(api_key),
        )

        if response.status_code != 200:
            raise Exception(f"Failed to poll batch: {response.status_code}")

        data = response.json()

    # Update batch record
    batch_record.status = data.get("processing_status", batch_record.status)
    counts = data.get("request_counts", {})
    batch_record.processing_count = counts.get("processing", 0)
    batch_record.succeeded_count = counts.get("succeeded", 0)
    batch_record.errored_count = counts.get("errored", 0)
    batch_record.expired_count = counts.get("expired", 0)
    batch_record.canceled_count = counts.get("canceled", 0)
    batch_record.results_url = data.get("results_url")

    if data.get("ended_at"):
        batch_record.ended_at = datetime.fromisoformat(data["ended_at"].replace("Z", "+00:00"))

    db.commit()
    db.refresh(batch_record)

    return batch_record


async def process_batch_results(db: Session, batch_id: str) -> dict:
    """
    Download and process batch results. Updates tenders with AI analysis.

    Args:
        db: Database session.
        batch_id: Anthropic batch ID.

    Returns:
        Summary dict with counts.
    """
    batch_record = db.query(MessageBatch).filter(MessageBatch.batch_id == batch_id).first()
    if not batch_record:
        raise ValueError(f"Batch {batch_id} not found")

    if batch_record.results_processed:
        return {"message": "Results already processed", "batch_id": batch_id}

    if batch_record.status not in ("ended",):
        raise ValueError(f"Batch is not ready for processing (status: {batch_record.status})")

    api_key = _get_api_key(db)

    # First, get the batch to ensure we have results_url
    if not batch_record.results_url:
        await poll_batch_status(db, batch_id)
        db.refresh(batch_record)

    if not batch_record.results_url:
        raise ValueError("No results URL available for this batch")

    # Download results (JSONL format, streamed)
    logger.info(f"Downloading results for batch {batch_id}")

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.get(
            batch_record.results_url,
            headers=_get_headers(api_key),
        )

        if response.status_code != 200:
            raise Exception(f"Failed to download results: {response.status_code}")

        results_text = response.text

    # Parse JSONL results
    succeeded = 0
    errored = 0
    expired = 0
    total_input_tokens = 0
    total_output_tokens = 0

    # Collect results by tender for atomic updates
    tender_results: dict[int, dict] = {}

    for line in results_text.strip().split("\n"):
        if not line.strip():
            continue

        try:
            result_data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse result line: {line[:100]}")
            continue

        custom_id = result_data.get("custom_id", "")
        result = result_data.get("result", {})
        result_type = result.get("type", "")

        # Find the batch item record
        item = db.query(MessageBatchItem).filter(
            MessageBatchItem.batch_id == batch_id,
            MessageBatchItem.custom_id == custom_id,
        ).first()

        if not item:
            logger.warning(f"No batch item found for custom_id: {custom_id}")
            continue

        if result_type == "succeeded":
            message = result.get("message", {})
            usage = message.get("usage", {})
            content = message.get("content", [])

            # Extract text (handles thinking blocks)
            text_parts = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            response_text = "\n".join(text_parts)

            item.result_status = "succeeded"
            item.result_text = response_text
            item.input_tokens = usage.get("input_tokens", 0)
            item.output_tokens = usage.get("output_tokens", 0)

            total_input_tokens += item.input_tokens
            total_output_tokens += item.output_tokens
            succeeded += 1

            # Collect for tender update
            if item.tender_id not in tender_results:
                tender_results[item.tender_id] = {}
            tender_results[item.tender_id][item.item_type] = response_text

        elif result_type == "errored":
            error = result.get("error", {})
            item.result_status = "errored"
            item.error_type = error.get("type", "unknown")
            item.error_message = error.get("message", str(error))
            errored += 1

        elif result_type == "canceled":
            item.result_status = "canceled"
            # canceled_count already tracked at batch level

        elif result_type == "expired":
            item.result_status = "expired"
            expired += 1

    # Apply results to tenders — scoring batches use the lean fan-out path;
    # deep-analysis batches use the 4-agent application below (unchanged).
    if batch_record.batch_type == "tender_scoring":
        # tender_results here is rep_id -> {"score": text}; flatten to rep_id -> text
        flat = {tid: parts.get("score", "") for tid, parts in tender_results.items()}
        tenders_updated = apply_scoring_results(db, batch_record, flat)
    else:
        tenders_updated = 0
        for tender_id, agent_results in tender_results.items():
            tender = db.query(Tender).filter(Tender.id == tender_id).first()
            if not tender:
                continue

            try:
                if "classifier" in agent_results:
                    category = agent_results["classifier"].strip()
                    valid = ["Mechanical", "Electrical", "Civil", "IT/Software",
                             "Materials/Supplies", "Consulting", "Other"]
                    tender.ai_category = category if category in valid else "Other"

                if "relevance" in agent_results:
                    try:
                        score = float(agent_results["relevance"].strip())
                        tender.ai_relevance_score = max(0.0, min(1.0, score))
                    except ValueError:
                        tender.ai_relevance_score = 0.5

                if "risk" in agent_results:
                    try:
                        score = float(agent_results["risk"].strip())
                        tender.ai_risk_score = max(0.0, min(1.0, score))
                    except ValueError:
                        tender.ai_risk_score = 0.5

                if "summary" in agent_results:
                    tender.ai_summary = agent_results["summary"].strip()[:500]

                tender.updated_at = datetime.now(timezone.utc)
                tenders_updated += 1
            except Exception as e:
                logger.error(f"Failed to apply results for tender {tender_id}: {e}")

    # Update batch record
    batch_record.results_processed = True
    batch_record.total_input_tokens = total_input_tokens
    batch_record.total_output_tokens = total_output_tokens
    batch_record.estimated_cost = _estimate_batch_cost(
        batch_record.model or "claude-sonnet-4-6",
        total_input_tokens,
        total_output_tokens,
    )

    # Log usage on a fresh session — telemetry must not poison the request session
    try:
        from app.core.database import SessionLocal
        _fresh_log_db = SessionLocal()
        try:
            usage_log = APIUsageLog(
                provider="anthropic",
                model=batch_record.model or "claude-sonnet-4-6",
                agent_name=f"batch_{batch_record.batch_type}",
                tokens_input=total_input_tokens,
                tokens_output=total_output_tokens,
                cost_estimate=batch_record.estimated_cost,
                success=True,
                response_time_ms=0,  # Batch processing — no single response time
            )
            _fresh_log_db.add(usage_log)
            _fresh_log_db.commit()
        finally:
            try:
                _fresh_log_db.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to log batch usage: {type(e).__name__}: {e}")

    db.commit()

    summary = {
        "batch_id": batch_id,
        "succeeded": succeeded,
        "errored": errored,
        "expired": expired,
        "tenders_updated": tenders_updated,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "estimated_cost": batch_record.estimated_cost,
    }

    logger.info(f"Batch {batch_id} results processed: {summary}")
    return summary


async def cancel_batch(db: Session, batch_id: str) -> MessageBatch:
    """
    Cancel an in-progress batch.

    Args:
        db: Database session.
        batch_id: Anthropic batch ID.

    Returns:
        Updated MessageBatch record.
    """
    batch_record = db.query(MessageBatch).filter(MessageBatch.batch_id == batch_id).first()
    if not batch_record:
        raise ValueError(f"Batch {batch_id} not found")

    if batch_record.status not in ("in_progress", "created"):
        raise ValueError(f"Cannot cancel batch with status: {batch_record.status}")

    api_key = _get_api_key(db)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{ANTHROPIC_BATCHES_URL}/{batch_id}/cancel",
            headers=_get_headers(api_key),
        )

        if response.status_code != 200:
            raise Exception(f"Failed to cancel batch: {response.status_code}")

        data = response.json()

    batch_record.status = data.get("processing_status", "canceling")
    db.commit()
    db.refresh(batch_record)

    return batch_record


def list_batches(
    db: Session,
    status: Optional[str] = None,
    batch_type: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[MessageBatch]:
    """List batch records from the database."""
    query = db.query(MessageBatch)
    if status:
        query = query.filter(MessageBatch.status == status)
    if batch_type:
        query = query.filter(MessageBatch.batch_type == batch_type)
    return query.order_by(MessageBatch.created_at.desc()).offset(offset).limit(limit).all()


def get_batch(db: Session, batch_id: str) -> Optional[MessageBatch]:
    """Get a single batch by Anthropic batch ID."""
    return db.query(MessageBatch).filter(MessageBatch.batch_id == batch_id).first()


def get_batch_items(db: Session, batch_id: str) -> list[MessageBatchItem]:
    """Get all items for a batch."""
    return db.query(MessageBatchItem).filter(
        MessageBatchItem.batch_id == batch_id
    ).order_by(MessageBatchItem.tender_id, MessageBatchItem.item_type).all()


def get_batch_stats(db: Session) -> dict:
    """Get overall batch processing statistics."""
    from sqlalchemy import func

    total_batches = db.query(MessageBatch).count()
    active_batches = db.query(MessageBatch).filter(
        MessageBatch.status.in_(["in_progress", "created", "canceling"])
    ).count()
    completed_batches = db.query(MessageBatch).filter(
        MessageBatch.status == "ended",
        MessageBatch.results_processed == True,
    ).count()

    total_cost = db.query(func.sum(MessageBatch.estimated_cost)).filter(
        MessageBatch.results_processed == True,
    ).scalar() or 0.0

    total_requests = db.query(func.sum(MessageBatch.total_requests)).scalar() or 0
    total_succeeded = db.query(func.sum(MessageBatch.succeeded_count)).scalar() or 0

    return {
        "total_batches": total_batches,
        "active_batches": active_batches,
        "completed_batches": completed_batches,
        "total_requests_submitted": total_requests,
        "total_requests_succeeded": total_succeeded,
        "total_cost_saved": round(total_cost, 4),  # This IS the batch cost (50% discount)
        "standard_cost_equivalent": round(total_cost * 2, 4),
    }


async def poll_and_process_if_ready(db: Session, batch_id: str) -> dict:
    """
    Convenience: poll a batch, and if it's ended, process results automatically.

    Returns status info dict.
    """
    batch = await poll_batch_status(db, batch_id)

    result = {
        "batch_id": batch_id,
        "status": batch.status,
        "processing_count": batch.processing_count,
        "succeeded_count": batch.succeeded_count,
        "errored_count": batch.errored_count,
        "total_requests": batch.total_requests,
        "results_processed": batch.results_processed,
    }

    if batch.status == "ended" and not batch.results_processed:
        process_result = await process_batch_results(db, batch_id)
        result["process_result"] = process_result
        result["results_processed"] = True

    return result
