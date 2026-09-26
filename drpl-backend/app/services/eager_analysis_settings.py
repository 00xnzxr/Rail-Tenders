"""DB-backed, live-read eager-analysis settings with config-default fallback.
Mirrors app/services/auto_scoring_settings.py."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.platform_setting import PlatformSetting

_KEYS = {
    "eager_analysis_enabled": "eager_analysis_enabled",
    "eager_analysis_min_score": "eager_analysis_min_score",
    "eager_analysis_interval_seconds": "eager_analysis_interval_seconds",
    "eager_analysis_batch_size": "eager_analysis_batch_size",
    "eager_analysis_max_concurrency": "eager_analysis_max_concurrency",
    "eager_analysis_daily_cap": "eager_analysis_daily_cap",
    "eager_analysis_queue_ceiling": "eager_analysis_queue_ceiling",
}


def _coerce(raw: str, default):
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw


def get_eager_analysis_settings(db: Session) -> dict:
    cfg = get_settings()
    rows = {r.key: r.value for r in db.query(PlatformSetting).filter(
        PlatformSetting.key.in_(list(_KEYS))).all()}
    out = {}
    for key, attr in _KEYS.items():
        default = getattr(cfg, attr)
        out[key] = _coerce(rows[key], default) if key in rows and rows[key] is not None else default
    return out
