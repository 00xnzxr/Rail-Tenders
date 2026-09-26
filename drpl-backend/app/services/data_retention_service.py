"""
DRPL Backend - Data Retention Service
Manages data retention policies and cleanup
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.data_retention import DataRetentionPolicy

DEFAULT_POLICIES = [
    {"data_type": "proposal_messages", "retention_days": 0, "description": "Chat messages in proposal sessions"},
    {"data_type": "scrape_logs", "retention_days": 90, "description": "Chrome extension scraping session logs"},
    {"data_type": "ai_analysis", "retention_days": 0, "description": "AI analysis results on tenders"},
    {"data_type": "tender_documents", "retention_days": 0, "description": "Uploaded tender documents"},
    {"data_type": "audit_logs", "retention_days": 365, "description": "Platform audit trail"},
    {"data_type": "api_usage_logs", "retention_days": 180, "description": "AI API usage tracking"},
]


def seed_defaults(db: Session):
    """Seed default retention policies."""
    for default in DEFAULT_POLICIES:
        existing = db.query(DataRetentionPolicy).filter(DataRetentionPolicy.data_type == default["data_type"]).first()
        if not existing:
            db.add(DataRetentionPolicy(
                data_type=default["data_type"],
                retention_days=default["retention_days"],
                description=default["description"],
                auto_delete=False,
                is_enabled=True,
            ))
    db.commit()


def get_policies(db: Session) -> list[DataRetentionPolicy]:
    """Get all retention policies."""
    seed_defaults(db)
    return db.query(DataRetentionPolicy).order_by(DataRetentionPolicy.data_type).all()


def update_policy(db: Session, policy_id: int, updates: dict, updated_by: int) -> Optional[DataRetentionPolicy]:
    """Update a retention policy."""
    policy = db.query(DataRetentionPolicy).filter(DataRetentionPolicy.id == policy_id).first()
    if not policy:
        return None
    for key, value in updates.items():
        if hasattr(policy, key) and key not in ("id", "data_type"):
            setattr(policy, key, value)
    policy.updated_by = updated_by
    policy.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(policy)
    return policy
