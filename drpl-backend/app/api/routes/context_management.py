"""
DRPL Backend - Context Management Routes
Endpoints for multi-provider context window management (Claude, OpenAI, Gemini):
- Token counting (pre-flight estimation)
- Context window info per model
- Cache performance stats
"""

from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Body
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.models.api_usage import APIUsageLog
from app.services.context_service import (
    get_model_context_info,
    get_all_models_context_info,
    count_tokens,
    resolve_context_config,
)

router = APIRouter(prefix="/context", tags=["context-management"])


@router.get("/models")
def list_model_context_info(
    current_user: User = Depends(get_current_user),
):
    """Get context window info for all known models (Claude, OpenAI, Gemini)."""
    return get_all_models_context_info()


@router.get("/models/{model_name}")
def get_single_model_context_info(
    model_name: str,
    current_user: User = Depends(get_current_user),
):
    """Get context window info for a specific model."""
    info = get_model_context_info(model_name)
    return info


@router.post("/count-tokens")
async def count_message_tokens(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Count tokens for a message before sending it.

    Free API — use for pre-flight validation, smart routing, and context management.

    Body should contain:
    - model: str (required)
    - messages: list[dict] (required)
    - system: str (optional)
    - tools: list[dict] (optional)
    - thinking: dict (optional)
    """
    model = body.get("model")
    messages = body.get("messages")

    if not model or not messages:
        raise HTTPException(status_code=400, detail="model and messages are required")

    result = await count_tokens(
        db=db,
        model=model,
        messages=messages,
        system=body.get("system"),
        tools=body.get("tools"),
        thinking=body.get("thinking"),
    )

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.get("/config")
def get_current_context_config(
    model: str = Query(None, description="Model to check capabilities for"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the current platform context management configuration.

    Returns resolved settings for compaction, caching, tool clearing, etc.
    """
    from app.services.ai_service import _get_effective_model
    effective_model = model or _get_effective_model(db, {})

    config = resolve_context_config(db, effective_model)
    model_info = get_model_context_info(effective_model)

    return {
        "model": effective_model,
        "model_info": model_info,
        "context_management": config.get("context_management"),
        "beta_headers": config.get("beta_headers", []),
        "prompt_caching_enabled": config.get("prompt_caching_enabled"),
        "cache_control": config.get("cache_control"),
    }


@router.get("/cache-stats")
def get_cache_statistics(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get prompt caching performance statistics across all providers.

    Shows cache hits, misses, and cost savings from prompt caching.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Total API calls across all providers
    total_calls = db.query(func.count(APIUsageLog.id)).filter(
        APIUsageLog.created_at >= cutoff,
        APIUsageLog.success == True,
    ).scalar() or 0

    total_input_tokens = db.query(func.sum(APIUsageLog.tokens_input)).filter(
        APIUsageLog.created_at >= cutoff,
        APIUsageLog.success == True,
    ).scalar() or 0

    total_output_tokens = db.query(func.sum(APIUsageLog.tokens_output)).filter(
        APIUsageLog.created_at >= cutoff,
        APIUsageLog.success == True,
    ).scalar() or 0

    total_cost = db.query(func.sum(APIUsageLog.cost_estimate)).filter(
        APIUsageLog.created_at >= cutoff,
        APIUsageLog.success == True,
    ).scalar() or 0.0

    # Cache metrics (if columns exist)
    cache_read_total = 0
    cache_creation_total = 0
    try:
        cache_read_total = db.query(func.sum(APIUsageLog.cache_read_tokens)).filter(
            APIUsageLog.created_at >= cutoff,
        ).scalar() or 0

        cache_creation_total = db.query(func.sum(APIUsageLog.cache_creation_tokens)).filter(
            APIUsageLog.created_at >= cutoff,
        ).scalar() or 0
    except Exception:
        pass  # Columns might not exist yet

    # Estimate cache savings (weighted average across providers)
    # Anthropic: 90% discount on reads, OpenAI: 50% discount on reads
    cache_savings = round(cache_read_total * 0.9 * 3.0 / 1_000_000, 4)

    # Per-provider breakdown, grouped from the rows rather than from a list of
    # provider names. The list used to be ["anthropic", "openai", "google"], so
    # a provider added later -- `runpod`, the self-hosted endpoint -- counted
    # towards the totals above and then vanished from the breakdown, which is
    # the one number an operator looks at to see whether the local model is
    # actually being used. One query instead of three per provider, too.
    by_provider = {}
    rows = (
        db.query(
            APIUsageLog.provider,
            func.count(APIUsageLog.id),
            func.sum(APIUsageLog.tokens_input),
            func.sum(APIUsageLog.cost_estimate),
        )
        .filter(
            APIUsageLog.created_at >= cutoff,
            APIUsageLog.success == True,  # noqa: E712
        )
        .group_by(APIUsageLog.provider)
        .all()
    )
    for provider, p_calls, p_input, p_cost in rows:
        if not provider or not p_calls:
            continue
        by_provider[provider] = {
            "api_calls": int(p_calls),
            "input_tokens": int(p_input or 0),
            "cost": round(float(p_cost or 0.0), 4),
        }

    return {
        "period_days": days,
        "total_api_calls": total_calls,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost": round(total_cost, 4),
        "cache_read_tokens": cache_read_total,
        "cache_creation_tokens": cache_creation_total,
        "estimated_cache_savings": cache_savings,
        "cache_hit_rate": round(
            cache_read_total / max(1, cache_read_total + cache_creation_total + total_input_tokens) * 100, 1
        ),
        "by_provider": by_provider,
    }
