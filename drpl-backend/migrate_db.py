"""
DRPL Backend - Database Migration Helper
Cross-database compatible (SQLite & PostgreSQL/Neon).
Adds new columns and tables to existing databases.

Usage: python migrate_db.py
"""

import sys
from sqlalchemy import inspect, text
from app.core.database import engine


COLUMN_MIGRATIONS = [
    # (table, column, sql)
    ("users", "last_login_at", "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP"),
    ("proposal_sessions", "title", "ALTER TABLE proposal_sessions ADD COLUMN title VARCHAR(500)"),
    ("proposal_sessions", "agent_type", "ALTER TABLE proposal_sessions ADD COLUMN agent_type VARCHAR(50) DEFAULT 'tender_proposal'"),
    ("proposal_sessions", "context_data", "ALTER TABLE proposal_sessions ADD COLUMN context_data TEXT"),
    ("tenders", "priority", "ALTER TABLE tenders ADD COLUMN priority VARCHAR(20) DEFAULT 'medium'"),
    ("tenders", "assigned_to", "ALTER TABLE tenders ADD COLUMN assigned_to INTEGER"),
    ("tenders", "assigned_at", "ALTER TABLE tenders ADD COLUMN assigned_at TIMESTAMP"),
    ("tenders", "submission_deadline", "ALTER TABLE tenders ADD COLUMN submission_deadline TIMESTAMP"),
    ("tenders", "workflow_status", "ALTER TABLE tenders ADD COLUMN workflow_status VARCHAR(50) DEFAULT 'new'"),
    ("tenders", "eligibility_status", "ALTER TABLE tenders ADD COLUMN eligibility_status VARCHAR(20)"),
    ("tenders", "eligibility_score", "ALTER TABLE tenders ADD COLUMN eligibility_score REAL"),
    ("tenders", "eligibility_notes", "ALTER TABLE tenders ADD COLUMN eligibility_notes TEXT"),
    ("tender_documents", "document_type", "ALTER TABLE tender_documents ADD COLUMN document_type VARCHAR(100)"),
    ("tender_documents", "checklist_item_id", "ALTER TABLE tender_documents ADD COLUMN checklist_item_id INTEGER"),
    ("agent_conversation_history", "output_type", "ALTER TABLE agent_conversation_history ADD COLUMN output_type VARCHAR(50)"),
    ("agent_conversation_history", "routed_from", "ALTER TABLE agent_conversation_history ADD COLUMN routed_from VARCHAR(100)"),
    # Extended Thinking & Effort columns
    ("agent_configs", "thinking_mode", "ALTER TABLE agent_configs ADD COLUMN thinking_mode VARCHAR(20)"),
    ("agent_configs", "thinking_budget_tokens", "ALTER TABLE agent_configs ADD COLUMN thinking_budget_tokens INTEGER"),
    ("agent_configs", "effort", "ALTER TABLE agent_configs ADD COLUMN effort VARCHAR(20)"),
    ("custom_agents", "thinking_mode", "ALTER TABLE custom_agents ADD COLUMN thinking_mode VARCHAR(20)"),
    ("custom_agents", "thinking_budget_tokens", "ALTER TABLE custom_agents ADD COLUMN thinking_budget_tokens INTEGER"),
    ("custom_agents", "effort", "ALTER TABLE custom_agents ADD COLUMN effort VARCHAR(20)"),
    # Prompt caching metrics
    ("api_usage_logs", "cache_read_tokens", "ALTER TABLE api_usage_logs ADD COLUMN cache_read_tokens INTEGER DEFAULT 0"),
    ("api_usage_logs", "cache_creation_tokens", "ALTER TABLE api_usage_logs ADD COLUMN cache_creation_tokens INTEGER DEFAULT 0"),
]

DATA_MIGRATIONS = [
    "UPDATE users SET role = 'master_admin' WHERE email = 'admin@drpl.com' AND role = 'admin'",
]


def column_exists(inspector, table: str, column: str) -> bool:
    """Check if a column exists in a table (cross-database)."""
    try:
        columns = [col["name"] for col in inspector.get_columns(table)]
        return column in columns
    except Exception:
        return False


def table_exists(inspector, table: str) -> bool:
    """Check if a table exists (cross-database)."""
    return table in inspector.get_table_names()


def main():
    inspector = inspect(engine)
    applied = 0
    skipped = 0

    # Create all tables from SQLAlchemy models (handles both SQLite and PostgreSQL)
    from app.core.database import Base
    from app.models import (  # noqa - register all models
        User, Tender, TenderDocument,
    )
    from app.models.checklist import ChecklistItem  # noqa
    from app.models.proposal import ProposalSession, ProposalMessage, ProposalDocument  # noqa
    from app.models.agent_config import AgentConfig  # noqa
    from app.models.redaction_rule import RedactionRule  # noqa
    from app.models.audit_log import AuditLog  # noqa
    from app.models.api_usage import APIUsageLog  # noqa
    from app.models.data_retention import DataRetentionPolicy  # noqa
    from app.models.platform_setting import PlatformSetting  # noqa
    from app.models.document_embedding import DocumentEmbedding  # noqa
    from app.models.message_batch import MessageBatch, MessageBatchItem  # noqa

    try:
        from app.models.proposal import ProposalReview  # noqa
    except ImportError:
        pass

    Base.metadata.create_all(bind=engine)
    print("[DRPL] All tables created/verified via SQLAlchemy models.")

    # Refresh inspector after table creation
    inspector = inspect(engine)

    # Add columns that may be missing on existing databases
    with engine.begin() as conn:
        for table, column, sql in COLUMN_MIGRATIONS:
            if not table_exists(inspector, table):
                print(f"  [skip] table {table} does not exist")
                skipped += 1
                continue
            if column_exists(inspector, table, column):
                print(f"  [skip] {table}.{column} already exists")
                skipped += 1
            else:
                try:
                    conn.execute(text(sql))
                    print(f"  [add]  {table}.{column}")
                    applied += 1
                except Exception as e:
                    print(f"  [error] {table}.{column}: {e}")
                    skipped += 1

        # Run data migrations
        for sql in DATA_MIGRATIONS:
            try:
                result = conn.execute(text(sql))
                if result.rowcount > 0:
                    print(f"  [data]  {sql[:60]}... ({result.rowcount} rows)")
                    applied += 1
            except Exception as e:
                print(f"  [skip] data migration: {e}")
                skipped += 1

    print(f"\nDone: {applied} changes applied, {skipped} already existed.")


if __name__ == "__main__":
    main()
