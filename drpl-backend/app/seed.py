"""
DRPL Backend - Database Seed Script
Creates initial master admin user, default settings, agent configs, and redaction rules.

Usage: python -m app.seed
"""

import os
import secrets

from passlib.context import CryptContext
from app.core.database import engine, SessionLocal, Base
from app.models import User  # noqa - registers models

# Import all models so SQLAlchemy creates their tables
from app.models.checklist import ChecklistItem  # noqa
from app.models.proposal import ProposalSession, ProposalMessage, ProposalDocument  # noqa
from app.models.agent_config import AgentConfig  # noqa
from app.models.redaction_rule import RedactionRule  # noqa
from app.models.audit_log import AuditLog  # noqa
from app.models.api_usage import APIUsageLog  # noqa
from app.models.data_retention import DataRetentionPolicy  # noqa
from app.models.platform_setting import PlatformSetting  # noqa

try:
    from app.models.proposal import ProposalReview  # noqa
except ImportError:
    pass

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def seed():
    # Create all tables
    Base.metadata.create_all(bind=engine)
    print("[DRPL] Database tables created.")

    db = SessionLocal()
    try:
        # --- 1. Master Admin User ---
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@drpl.com")
        admin_password = os.environ.get("ADMIN_PASSWORD", "")

        existing = db.query(User).filter(User.email == admin_email).first()
        if existing:
            if existing.role == "admin":
                existing.role = "master_admin"
                db.commit()
                print(f"[DRPL] Upgraded {admin_email} from 'admin' to 'master_admin'.")
            else:
                print(f"[DRPL] Admin user already exists with role '{existing.role}'.")
        else:
            # Generate random password if not provided
            if not admin_password:
                admin_password = f"drpl-{secrets.token_urlsafe(12)}"
                print(f"[DRPL] Generated admin password: {admin_password}")
                print("[DRPL] WARNING: Save this password now — it will not be shown again!")

            admin = User(
                email=admin_email,
                name="DRPL Master Admin",
                hashed_password=pwd_context.hash(admin_password),
                role="master_admin",
                is_active=True,
            )
            db.add(admin)
            db.commit()
            print(f"[DRPL] Master admin user created: {admin_email}")

        # --- 2. Default Platform Settings ---
        from app.services.settings_service import seed_defaults
        seed_defaults(db)
        print("[DRPL] Platform settings seeded.")

        # --- 3. Default Agent Configs ---
        from app.services.agent_config_service import seed_agents
        seed_agents(db)
        print("[DRPL] AI agent configs seeded.")

        # --- 4. System Redaction Rules ---
        _seed_redaction_rules(db)
        print("[DRPL] Redaction rules seeded.")

        # --- 5. System Agents (Agent Builder records) ---
        from app.services.seed_costing_researcher_agent import seed_costing_researcher
        seed_costing_researcher(db)
        try:
            from app.services.seed_costing_scope_extractor_agent import seed_costing_scope_extractor
            seed_costing_scope_extractor(db)
        except Exception as e:
            print(f"[DRPL WARN] costing_scope_extractor seed failed (non-fatal): {e}")
        try:
            from app.services.seed_tender_analyzer_agent import seed_tender_analyzer
            seed_tender_analyzer(db)
        except Exception:
            pass
        from app.services.agent_builder_service import seed_system_agents
        seed_system_agents(db)
        print("[DRPL] System agents seeded.")

        print("[DRPL] Seed complete.")

    finally:
        db.close()


def _seed_redaction_rules(db):
    """Seed default system redaction rules if not present."""
    SYSTEM_RULES = [
        {"name": "PAN Card", "pattern": r"[A-Z]{5}\d{4}[A-Z]", "replacement": "[PAN REDACTED]", "category": "PII"},
        {"name": "Aadhaar Number", "pattern": r"\d{4}\s?\d{4}\s?\d{4}", "replacement": "[AADHAAR REDACTED]", "category": "PII"},
        {"name": "Phone Number", "pattern": r"(?:\+91[\s-]?)?[6-9]\d{9}", "replacement": "[PHONE REDACTED]", "category": "PII"},
        {"name": "IFSC Code", "pattern": r"[A-Z]{4}0[A-Z0-9]{6}", "replacement": "[IFSC REDACTED]", "category": "Financial"},
        {"name": "GSTIN", "pattern": r"\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9]", "replacement": "[GSTIN REDACTED]", "category": "Financial"},
        {"name": "Bank Account", "pattern": r"\d{9,18}", "replacement": "[ACCOUNT REDACTED]", "category": "Financial"},
    ]

    for rule_data in SYSTEM_RULES:
        existing = db.query(RedactionRule).filter(RedactionRule.name == rule_data["name"], RedactionRule.is_system == True).first()
        if not existing:
            rule = RedactionRule(
                name=rule_data["name"],
                pattern=rule_data["pattern"],
                replacement=rule_data["replacement"],
                category=rule_data["category"],
                is_system=True,
                is_enabled=True,
            )
            db.add(rule)
    db.commit()


if __name__ == "__main__":
    seed()
