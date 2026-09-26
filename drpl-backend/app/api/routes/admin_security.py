"""
DRPL Backend - Admin Security Routes
Redaction rules and data security settings (master_admin only)
"""

import re
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.services.audit_service import log_action
from app.services.data_retention_service import get_policies, update_policy
from app.models.user import User
from app.models.redaction_rule import RedactionRule

router = APIRouter(prefix="/admin/security", tags=["admin-security"])

# System default rules to seed
SYSTEM_RULES = [
    {"name": "PAN Card", "pattern": r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", "replacement": "[REDACTED-PAN]", "category": "pii"},
    {"name": "Aadhaar Number", "pattern": r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b", "replacement": "[REDACTED-AADHAAR]", "category": "pii"},
    {"name": "Phone Number", "pattern": r"\b(?:\+91[\-\s]?)?[6-9]\d{9}\b", "replacement": "[REDACTED-PHONE]", "category": "pii"},
    {"name": "IFSC Code", "pattern": r"\b[A-Z]{4}0[A-Z0-9]{6}\b", "replacement": "[REDACTED-IFSC]", "category": "financial"},
    {"name": "GSTIN", "pattern": r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z0-9]{2}\b", "replacement": "[REDACTED-GST]", "category": "financial"},
    {"name": "Bank Account", "pattern": r"\b\d{9,18}\b(?=.*(?:account|a/c|bank))", "replacement": "[REDACTED-BANK-ACCOUNT]", "category": "financial"},
]


def seed_rules(db: Session):
    """Seed system redaction rules."""
    for rule in SYSTEM_RULES:
        existing = db.query(RedactionRule).filter(RedactionRule.name == rule["name"], RedactionRule.is_system == True).first()
        if not existing:
            db.add(RedactionRule(
                name=rule["name"], pattern=rule["pattern"], replacement=rule["replacement"],
                category=rule["category"], is_system=True, is_enabled=True,
            ))
    db.commit()


class RedactionRuleResponse(BaseModel):
    id: int
    name: str
    pattern: str
    replacement: str
    category: str
    is_enabled: bool
    is_system: bool
    description: Optional[str]

    class Config:
        from_attributes = True


class CreateRuleInput(BaseModel):
    name: str
    pattern: str
    replacement: str
    category: str = "custom"
    description: str = ""

class UpdateRuleInput(BaseModel):
    name: Optional[str] = None
    pattern: Optional[str] = None
    replacement: Optional[str] = None
    is_enabled: Optional[bool] = None
    description: Optional[str] = None

class TestPatternInput(BaseModel):
    pattern: str
    sample_text: str


@router.get("/redaction-rules", response_model=list[RedactionRuleResponse])
def list_redaction_rules(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all redaction rules."""
    seed_rules(db)
    return db.query(RedactionRule).order_by(RedactionRule.is_system.desc(), RedactionRule.name).all()


@router.post("/redaction-rules", response_model=RedactionRuleResponse)
def create_redaction_rule(
    body: CreateRuleInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Create a custom redaction rule."""
    # Validate regex
    try:
        re.compile(body.pattern)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex pattern: {e}")

    rule = RedactionRule(
        name=body.name, pattern=body.pattern, replacement=body.replacement,
        category=body.category, description=body.description,
        is_system=False, is_enabled=True, created_by=admin.id,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    log_action(db, admin.id, admin.email, "redaction_rule.created", "redaction_rule", str(rule.id), {"name": body.name})
    return rule


@router.put("/redaction-rules/{rule_id}", response_model=RedactionRuleResponse)
def update_redaction_rule(
    rule_id: int,
    body: UpdateRuleInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update a redaction rule."""
    rule = db.query(RedactionRule).filter(RedactionRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    if body.pattern is not None:
        try:
            re.compile(body.pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regex pattern: {e}")
        rule.pattern = body.pattern

    if body.name is not None:
        rule.name = body.name
    if body.replacement is not None:
        rule.replacement = body.replacement
    if body.is_enabled is not None:
        rule.is_enabled = body.is_enabled
    if body.description is not None:
        rule.description = body.description

    rule.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(rule)
    log_action(db, admin.id, admin.email, "redaction_rule.updated", "redaction_rule", str(rule_id))
    return rule


@router.delete("/redaction-rules/{rule_id}")
def delete_redaction_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Delete a custom redaction rule (system rules cannot be deleted)."""
    rule = db.query(RedactionRule).filter(RedactionRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    if rule.is_system:
        raise HTTPException(status_code=400, detail="System rules cannot be deleted. Disable them instead.")
    db.delete(rule)
    db.commit()
    log_action(db, admin.id, admin.email, "redaction_rule.deleted", "redaction_rule", str(rule_id))
    return {"status": "deleted"}


@router.post("/redaction-rules/test")
def test_redaction_pattern(
    body: TestPatternInput,
    admin: User = Depends(require_master_admin),
):
    """Test a regex pattern against sample text."""
    try:
        compiled = re.compile(body.pattern, re.IGNORECASE)
        matches = compiled.findall(body.sample_text)
        redacted = compiled.sub("[REDACTED]", body.sample_text)
        return {"matches": matches, "match_count": len(matches), "redacted_text": redacted}
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {e}")


# --- Retention Policies ---

class RetentionPolicyResponse(BaseModel):
    id: int
    data_type: str
    retention_days: int
    auto_delete: bool
    is_enabled: bool
    description: Optional[str]

    class Config:
        from_attributes = True

class UpdateRetentionInput(BaseModel):
    retention_days: Optional[int] = None
    auto_delete: Optional[bool] = None
    is_enabled: Optional[bool] = None


@router.get("/retention-policies", response_model=list[RetentionPolicyResponse])
def list_retention_policies(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all data retention policies."""
    return get_policies(db)


@router.put("/retention-policies/{policy_id}", response_model=RetentionPolicyResponse)
def update_retention_policy(
    policy_id: int,
    body: UpdateRetentionInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update a retention policy."""
    updates = body.model_dump(exclude_none=True)
    policy = update_policy(db, policy_id, updates, admin.id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    log_action(db, admin.id, admin.email, "retention_policy.updated", "retention_policy", str(policy_id), updates)
    return policy
