"""Admin control panel for the auto tender-scoring agent."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting
from app.services.auto_scoring_settings import get_scoring_settings, set_scoring_setting, _KEYS
from app.services.auto_scoring_service import resegment_all
from app.services.seed_scoring_agent import get_scoring_system_prompt
from app.services.scoring_backlog_service import backlog_stats, drain_backlog

router = APIRouter(prefix="/admin/tender-scoring", tags=["tender-scoring-admin"])


@router.get("/settings")
def get_settings_route(db: Session = Depends(get_db), _: User = Depends(require_master_admin)):
    return get_scoring_settings(db)


@router.put("/settings")
def put_settings_route(
    body: dict,
    resegment: bool = Query(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_master_admin),
):
    for key, value in body.items():
        if key in _KEYS:
            set_scoring_setting(db, key, value, updated_by=getattr(user, "id", None))
    result = get_scoring_settings(db)
    if resegment:
        result["_resegmented"] = resegment_all(db)["resegmented"]
    return result


class DrainRequest(BaseModel):
    mode: str = "live"          # "live" | "batch"
    limit: "int | None" = None


@router.get("/backlog")
def backlog_route(db: Session = Depends(get_db), _: User = Depends(require_master_admin)):
    return backlog_stats(db)


@router.post("/drain")
def drain_route(
    body: DrainRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_master_admin),
):
    return drain_backlog(db, mode=body.mode, limit=body.limit)


@router.get("/stats")
def stats_route(db: Session = Depends(get_db), _: User = Depends(require_master_admin)):
    rows = dict(db.query(Tender.segment, func.count(Tender.id)).group_by(Tender.segment).all())
    total_scored = db.query(func.count(Tender.id)).filter(Tender.ai_relevance_score.isnot(None)).scalar()
    backlog = db.query(func.count(Tender.id)).filter(Tender.ai_relevance_score.is_(None)).scalar()
    last = db.query(PlatformSetting).filter(PlatformSetting.key == "auto_scoring_last_run").first()
    return {
        "total_scored": total_scored,
        "unscored_backlog": backlog,
        "last_reaper_run": last.value if last else None,
        "counts": {
            "to_bid": rows.get("to_bid", 0),
            "not_bidable": rows.get("not_bidable", 0),
            "discarded": rows.get("discarded", 0),
            "unscored": rows.get(None, 0),
        },
    }


@router.get("/digest")
def digest_route(db: Session = Depends(get_db), _: User = Depends(require_master_admin)):
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "auto_scoring_digest").first()
    return {"digest": row.value if row else None}


@router.post("/digest/regenerate")
def regenerate_digest_route(db: Session = Depends(get_db), _: User = Depends(require_master_admin)):
    # Clear the stored hash so the next build re-distills, then rebuild now.
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "auto_scoring_digest_hash").first()
    if row is not None:
        db.delete(row)
        db.commit()
    prompt = get_scoring_system_prompt(db)
    return {"digest": prompt}
