"""
DRPL Backend - SQLite to PostgreSQL Data Transfer
Migrates all data from the local SQLite database to the cloud PostgreSQL database.

Usage: python transfer_data.py
"""

import sys
from sqlalchemy import create_engine, inspect, text, MetaData
from sqlalchemy.orm import sessionmaker

# Target: PostgreSQL from app config (reads DATABASE_URL from .env)
from app.core.config import get_settings
from app.core.database import Base

# Import all models so SQLAlchemy knows about them
from app.models import User, Tender, TenderDocument  # noqa
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
from app.models.agent_memory import AgentMemory, AgentConversationHistory  # noqa
from app.models.agent_builder import CustomAgent  # noqa
from app.models.api_token import APIToken  # noqa
from app.models.mcp_config import MCPServerConfig  # noqa
from app.models.letterhead import LetterheadTemplate, DigitalSignature, GeneratedDocument  # noqa
from app.models.document_analysis import DocumentExtractionResult, CriticalClauseFlag, ExtractionFeedback, TenderAnalysisSummary  # noqa
from app.models.proposal_template import ProposalTemplate  # noqa

try:
    from app.models.proposal import ProposalReview  # noqa
except ImportError:
    pass

try:
    from app.models.agent_builder import AgentVersion, AgentTool, AgentTestCase, AgentTestRun, AgentExecution  # noqa
except ImportError:
    pass

try:
    from app.models.tender import ScrapeLog  # noqa
except ImportError:
    pass

# Source: local SQLite
SQLITE_URL = "sqlite:///./drpl_local.db"


def transfer():
    settings = get_settings()
    target_url = settings.database_url

    if target_url.startswith("sqlite"):
        print("[ERROR] DATABASE_URL in .env still points to SQLite.")
        print("        Update it to your PostgreSQL connection string first.")
        sys.exit(1)

    print(f"[DRPL] Source: {SQLITE_URL}")
    print(f"[DRPL] Target: {target_url[:50]}...")
    print()

    # Connect to source SQLite
    src_engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
    src_session = sessionmaker(bind=src_engine)()

    # Connect to target PostgreSQL
    tgt_engine = create_engine(target_url, pool_pre_ping=True)
    tgt_session = sessionmaker(bind=tgt_engine)()

    # Create all tables on target
    print("[DRPL] Creating tables on target database...")
    Base.metadata.create_all(bind=tgt_engine)
    print("[DRPL] Tables created.")
    print()

    # Get list of tables from SQLite
    src_inspector = inspect(src_engine)
    src_tables = src_inspector.get_table_names()

    tgt_inspector = inspect(tgt_engine)
    tgt_tables = tgt_inspector.get_table_names()

    total_rows = 0

    for table_name in src_tables:
        if table_name.startswith("alembic"):
            continue

        if table_name not in tgt_tables:
            print(f"  [skip] {table_name} — not in target schema")
            continue

        try:
            # Read all rows from source
            rows = src_session.execute(text(f'SELECT * FROM "{table_name}"')).fetchall()
            if not rows:
                print(f"  [skip] {table_name} — empty")
                continue

            # Get column names
            columns = [col["name"] for col in src_inspector.get_columns(table_name)]
            tgt_columns = [col["name"] for col in tgt_inspector.get_columns(table_name)]

            # Only transfer columns that exist in both source and target
            common_columns = [c for c in columns if c in tgt_columns]

            # Check existing row count in target
            existing_count = tgt_session.execute(
                text(f'SELECT COUNT(*) FROM "{table_name}"')
            ).scalar()

            if existing_count > 0:
                print(f"  [skip] {table_name} — target already has {existing_count} rows")
                continue

            # Build insert statement
            col_list = ", ".join(f'"{c}"' for c in common_columns)
            param_list = ", ".join(f":{c}" for c in common_columns)
            insert_sql = text(
                f'INSERT INTO "{table_name}" ({col_list}) VALUES ({param_list})'
            )

            # Insert rows in batches
            batch_size = 100
            inserted = 0
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                row_dicts = []
                for row in batch:
                    row_dict = {}
                    for col in common_columns:
                        idx = columns.index(col)
                        row_dict[col] = row[idx]
                    row_dicts.append(row_dict)

                tgt_session.execute(insert_sql, row_dicts)
                inserted += len(batch)

            tgt_session.commit()
            total_rows += inserted
            print(f"  [done] {table_name} — {inserted} rows transferred")

        except Exception as e:
            tgt_session.rollback()
            print(f"  [error] {table_name} — {e}")

    print()
    print(f"[DRPL] Transfer complete: {total_rows} total rows migrated.")

    src_session.close()
    tgt_session.close()


if __name__ == "__main__":
    transfer()
