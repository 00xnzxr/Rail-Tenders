"""DB-backed, live-read scoring/segmentation settings with config-default fallback."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.platform_setting import PlatformSetting

# key -> config attribute providing the default
_KEYS = {
    "auto_scoring_enabled": "auto_scoring_enabled",
    "auto_scoring_model": "auto_scoring_model",
    "auto_scoring_interval_seconds": "auto_scoring_interval_seconds",
    "auto_scoring_batch_size": "auto_scoring_batch_size",
    "auto_scoring_max_retries": "auto_scoring_max_retries",
    "auto_scoring_max_concurrency": "auto_scoring_max_concurrency",
    "value_threshold_inr": "value_threshold_inr",
    "segment_discard_below": "segment_discard_below",
    "segment_bidable_at": "segment_bidable_at",
    "auto_discard_enabled": "auto_discard_enabled",
    "auto_discard_days": "auto_discard_days",
    "auto_discard_interval_hours": "auto_discard_interval_hours",
    "archive_sweep_enabled": "archive_sweep_enabled",
    "archive_grace_days": "archive_grace_days",
    "archive_purge_days": "archive_purge_days",
    "archive_sweep_interval_hours": "archive_sweep_interval_hours",
    "archive_sweep_batch_size": "archive_sweep_batch_size",
}


def _coerce(raw: str, default):
    """Coerce a stored string back to the type of its config default."""
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw


def get_scoring_settings(db: Session) -> dict:
    cfg = get_settings()
    rows = {r.key: r.value for r in db.query(PlatformSetting).filter(
        PlatformSetting.key.in_(list(_KEYS))).all()}
    out = {}
    for key, attr in _KEYS.items():
        default = getattr(cfg, attr)
        out[key] = _coerce(rows[key], default) if key in rows and rows[key] is not None else default
    return out


def set_scoring_setting(db: Session, key: str, value, updated_by: "int | None" = None) -> None:
    if key not in _KEYS:
        raise ValueError(f"unknown scoring setting: {key}")
    stored = "true" if value is True else "false" if value is False else str(value)
    row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
    if row is None:
        row = PlatformSetting(key=key, value=stored, value_type="string", category="auto_scoring")
        if updated_by is not None and hasattr(row, "updated_by"):
            row.updated_by = updated_by
        db.add(row)
    else:
        row.value = stored
        if updated_by is not None and hasattr(row, "updated_by"):
            row.updated_by = updated_by
    db.commit()
