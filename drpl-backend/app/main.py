"""
DRPL Backend - Main Application
FastAPI entry point with all routers mounted
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Silence SQLAlchemy echo duplicates (engine.echo already logs SQL)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)

import os as _os
_os.makedirs("logs", exist_ok=True)

# Full-firehose file mirror of the backend's console output. basicConfig only
# attaches a console StreamHandler (goes to the uvicorn terminal); this adds a
# size-bounded file copy so the complete backend log can be tailed from a side
# process (`tail -f logs/backend.log`) to see exactly what is running —
# requests, agent runs, storage/BOQ extraction, tracebacks — without scraping
# stdout. Rotates at ~20MB × 4 files.
from logging.handlers import RotatingFileHandler as _RotatingFileHandler


class _ExcludeNoisyLoggers(logging.Filter):
    """Keep backend.log readable + cheap: drop the high-volume SQLAlchemy
    echo SQL firehose and per-request access logs. Without this, echo=True
    writes every SQL statement to disk synchronously (hundreds on startup),
    burying the app logs and slowing the server. WARNING+ from these loggers
    still passes (so real DB/connection errors are not hidden)."""

    _NOISY = ("sqlalchemy.engine", "sqlalchemy.pool", "uvicorn.access")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not record.name.startswith(self._NOISY)


_backend_handler = _RotatingFileHandler(
    "logs/backend.log", mode="a", maxBytes=20_000_000, backupCount=4, encoding="utf-8",
)
_backend_handler.setLevel(logging.INFO)
_backend_handler.addFilter(_ExcludeNoisyLoggers())
_backend_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
# Guard against duplicate handlers across uvicorn --reload restarts.
_root_logger = logging.getLogger()
if not any(
    isinstance(h, _RotatingFileHandler)
    and getattr(h, "baseFilename", "").endswith("backend.log")
    for h in _root_logger.handlers
):
    _root_logger.addHandler(_backend_handler)

# Dedicated file logger for costing-pipeline observability — lets a side
# process tail logs/costing_run.log to see agent invocations, tool calls,
# parse errors, and persistence outcomes without scraping the uvicorn stdout.
_costing_handler = logging.FileHandler("logs/costing_run.log", mode="a", encoding="utf-8")
_costing_handler.setLevel(logging.INFO)
_costing_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
for _logger_name in (
    "app.services.langchain.graphs.costing_agent",
    "app.services.langchain.graphs.enhanced_costing_agent",
    "app.services.langchain.graphs.chat_agent_wrappers",
    "app.services.langchain.tools.cost_calculator_tool",
    "app.services.langchain.tools.xlsx_generator_tool",
    "app.services.langchain.tools.anonymizing_web_search_tool",
    "app.services.langchain.tools.costing_training_retrieval_tool",
    "app.services.cost_breakdown_service",
    "app.services.boq_parser_service",
    "app.services.storage_service",
    "app.services.langchain.canonical_registry",
    "app.api.routes.command_center",
    # Analyzer + annexure loggers so the full pipeline (analyzer → costing
    # → annexure) lands in one file for end-to-end run observation.
    "app.services.langchain.graphs.document_analysis_agent",
    "app.services.tender_enrichment_service",
    "app.services.langchain.graphs.annexure_finder_agent",
    "app.services.langchain.graphs.costing_scope_extractor",
    "app.services.langchain.graphs.decision_maker_agent",
):
    logging.getLogger(_logger_name).addHandler(_costing_handler)

# Stamp [run=<first8>] onto every log record emitted inside a run_id_scope.
from app.core.run_context import install_run_id_filter
install_run_id_filter()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, extension, tenders, monitoring, checklist, proposals
from app.api.routes import collect
from app.api.routes import admin_settings, admin_users, admin_security, admin_audit, admin_dashboard
from app.api.routes import usage
from app.api.routes import admin_scope_profile
from app.api.routes import tender_scoring_admin
from app.api.routes import templates, training_datasets, ratecards
from app.api.routes import document_analysis
from app.api.routes import letterhead, signatures, documents, offline_documents
from app.api.routes import agent_builder
from app.api.routes import langchain_agents
from app.api.routes import batch_processing
from app.api.routes import context_management
from app.api.routes import command_center
from app.api.routes import workspace
from app.api.routes import workflows
from app.api.routes import cost_breakdown
from app.api.routes import cost_breakdowns
from app.api.routes import runs
from app.api.routes import notifications, admin_notifications
from app.core.config import get_settings
from app.core.database import engine, Base

# Import all models so SQLAlchemy knows about them
from app.models.user import User
from app.models.tender import Tender, TenderDocument, ScrapeLog
from app.models.tender_scope_profile import TenderScopeProfile
from app.models.api_token import APIToken
from app.models.checklist import ChecklistItem
from app.models.proposal import ProposalSession, ProposalMessage, ProposalDocument, ProposalReview
from app.models.platform_setting import PlatformSetting
from app.models.agent_config import AgentConfig
from app.models.redaction_rule import RedactionRule
from app.models.audit_log import AuditLog
from app.models.api_usage import APIUsageLog
from app.models.data_retention import DataRetentionPolicy
from app.models.proposal_template import ProposalTemplate
from app.models.document_analysis import DocumentExtractionResult, CriticalClauseFlag, ExtractionFeedback, TenderAnalysisSummary
from app.models.letterhead import LetterheadTemplate, DigitalSignature, GeneratedDocument
from app.models.agent_builder import CustomAgent, AgentVersion, AgentTool, AgentTestCase, AgentTestRun, AgentExecution, AgentTestDocument
from app.models.agent_memory import AgentMemory, AgentConversationHistory
from app.models.mcp_config import MCPServerConfig
from app.models.message_batch import MessageBatch, MessageBatchItem
from app.models.training_dataset import TrainingDataset, TrainingDatasetFile, AgentTrainingDataset
from app.models.workspace import WorkspaceConfig, DocumentWorkspace, DocumentFormatTemplate
from app.models.workflow import (
    Workflow, WorkflowVersion, WorkflowNode, WorkflowEdge,
    WorkflowExecution, WorkflowNodeExecution,
)
from app.models.notification import Notification, NotificationPreference

settings = get_settings()

# Create all tables
Base.metadata.create_all(bind=engine)

# Drift correction — apply column additions that Base.metadata.create_all() cannot.
# Postgres IF NOT EXISTS makes this a no-op once applied.
def _apply_schema_drift_fixes():
    from sqlalchemy import text
    _log = logging.getLogger(__name__)
    statements = [
        # Ownership column the per-user firewall scopes on (app/core/ownership.py).
        # cost_breakdowns was keyed only by tender + version, so two costing
        # researchers on the same tender shared one row's numbers.
        "ALTER TABLE cost_breakdowns ADD COLUMN IF NOT EXISTS created_by INTEGER",
        # Which run each LLM call belonged to — see run_cost_service.
        "ALTER TABLE api_usage_logs ADD COLUMN IF NOT EXISTS run_id VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_cost_breakdowns_created_by ON cost_breakdowns(created_by)",
        "ALTER TABLE tender_documents ADD COLUMN IF NOT EXISTS gem_file_id VARCHAR(50)",
        "CREATE INDEX IF NOT EXISTS ix_tender_documents_gem_file_id ON tender_documents(gem_file_id)",
        # Phase 7 — scope-profile driven scraping
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS search_match_keyword VARCHAR(255)",
        "CREATE INDEX IF NOT EXISTS ix_tenders_search_match_keyword ON tenders(search_match_keyword)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS is_eligible_indicator BOOLEAN",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS fit_reasoning TEXT",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT false",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS location VARCHAR(255)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS bid_type VARCHAR(50)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS source_portal VARCHAR(50)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS category VARCHAR(255)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS below_threshold BOOLEAN DEFAULT false",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS scoring_attempts INTEGER DEFAULT 0",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS segment VARCHAR(20)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS segment_overridden BOOLEAN DEFAULT false",
        # Phase 7 follow-up — per-keyword pagination override on the scope profile
        "ALTER TABLE tender_scope_profiles ADD COLUMN IF NOT EXISTS max_pages_per_keyword INTEGER NOT NULL DEFAULT 5",
        # Pre-existing schema drift on tender_analysis_summaries (cost-tracking columns)
        "ALTER TABLE tender_analysis_summaries ADD COLUMN IF NOT EXISTS total_input_tokens INTEGER",
        "ALTER TABLE tender_analysis_summaries ADD COLUMN IF NOT EXISTS total_output_tokens INTEGER",
        "ALTER TABLE tender_analysis_summaries ADD COLUMN IF NOT EXISTS total_cost_usd NUMERIC(10, 4)",
        "ALTER TABLE tender_analysis_summaries ADD COLUMN IF NOT EXISTS analysis_version VARCHAR(8)",
        "ALTER TABLE tender_analysis_summaries ADD COLUMN IF NOT EXISTS per_doc_unreadable_count INTEGER",
        # Raw-markdown-only tender analysis: drop the duplicated structured JSON column.
        # The artifact panel now renders requirement_summary (markdown) directly.
        "ALTER TABLE tender_analysis_summaries DROP COLUMN IF EXISTS structured_analysis",
        # Pre-existing schema drift on document_extraction_results
        "ALTER TABLE document_extraction_results ADD COLUMN IF NOT EXISTS summary_json JSON",
        # NIT-aware costing — extend BOQItem to be a faithful NIT-schedule row.
        # See plan: now-i-need-to-synchronous-taco.md (data model section).
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS item_code VARCHAR(64)",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS schedule_name VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_boq_items_schedule_name ON boq_items(schedule_name)",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS bidding_unit VARCHAR(64)",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS basic_value DOUBLE PRECISION",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS escalation_pct DOUBLE PRECISION",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS is_tax_line BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS extraction_confidence VARCHAR(16)",
        # CostBreakdownLine — mirror of the NIT row + FK back to its source BOQItem.
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS boq_item_id INTEGER REFERENCES boq_items(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_cost_breakdown_lines_boq_item_id ON cost_breakdown_lines(boq_item_id)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS item_code VARCHAR(64)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS schedule_name VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_cost_breakdown_lines_schedule_name ON cost_breakdown_lines(schedule_name)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS bidding_unit VARCHAR(64)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS basic_value DOUBLE PRECISION",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS escalation_pct DOUBLE PRECISION",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS is_tax_line BOOLEAN NOT NULL DEFAULT false",
        # Component build-up costing (ratecard-driven) — annexure grouping +
        # selectable output layout.
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS annexure VARCHAR(8)",
        "CREATE INDEX IF NOT EXISTS ix_cost_breakdown_lines_annexure ON cost_breakdown_lines(annexure)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS oem_manufacturer VARCHAR(128)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS source_url TEXT",
        "ALTER TABLE cost_breakdowns ADD COLUMN IF NOT EXISTS cost_sheet_template VARCHAR(32)",
        # Offline document signing — original (clean) uploaded PDF for re-sign.
        "ALTER TABLE generated_documents ADD COLUMN IF NOT EXISTS source_file_path TEXT",
        # Reconciliation gate — sum(line amounts) vs the tender's own stated
        # schedule totals (BOQScheduleTotal). Flag only, never mutates lines.
        "ALTER TABLE cost_breakdowns ADD COLUMN IF NOT EXISTS reconciliation_json TEXT",
        "ALTER TABLE cost_breakdowns ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT false",
        # Eager analysis (pre-fetch pipeline warmup) — per-tender in-flight state
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS eager_analysis_status VARCHAR(16)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS eager_analysis_at TIMESTAMPTZ",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS archive_reason VARCHAR(30)",
        "ALTER TABLE boq_schedule_totals ADD COLUMN IF NOT EXISTS reconciliation_status VARCHAR(16)",
        # The schedule's banner as printed (material / labour / both, and
        # whether its rates include GST). Read by the costing build-up.
        "ALTER TABLE boq_schedule_totals ADD COLUMN IF NOT EXISTS title TEXT",
        # Annexure letterhead opt-out. NULL letterhead_template_id now means
        # "inherit the tender default"; this flag is the explicit "none" (e.g. a
        # bank guarantee bond, which belongs on the bank's letterhead).
        "ALTER TABLE document_workspaces ADD COLUMN IF NOT EXISTS letterhead_disabled BOOLEAN NOT NULL DEFAULT false",
        "CREATE INDEX IF NOT EXISTS ix_tenders_eager_analysis_status ON tenders(eager_analysis_status)",
        "CREATE INDEX IF NOT EXISTS ix_tenders_archived_at ON tenders(archived_at)",
        # latency_ms was INTEGER. The startup reaper back-fills it with
        # (now - created_at) for rows orphaned at status="running"; a row stuck
        # for months exceeds 2^31-1 and fails the whole batch UPDATE, which
        # strands every stuck row on every subsequent boot. Idempotent: a
        # no-op once the column is already BIGINT.
        "ALTER TABLE agent_executions ALTER COLUMN latency_ms TYPE BIGINT",
        # What a run had streamed so far, written by the worker as it goes, so
        # a run that dies mid-way leaves something readable after Redis's
        # one-hour stream expiry. See AgentRun.partial_output.
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS partial_output TEXT",
        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS partial_trace JSON",
        # Annexure components: a schedule item's cited annexure rows are
        # captured as BOQItems linked to the item they break down, so they
        # roll up into it instead of being costed on top of it. See
        # BOQItem.parent_item_id / CostBreakdownLine.parent_boq_item_id.
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS annexure_ref VARCHAR(32)",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS parent_item_id INTEGER",
        "ALTER TABLE boq_items ADD COLUMN IF NOT EXISTS source_document_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_boq_items_annexure_ref ON boq_items(annexure_ref)",
        "CREATE INDEX IF NOT EXISTS ix_boq_items_parent_item_id ON boq_items(parent_item_id)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS parent_boq_item_id INTEGER REFERENCES boq_items(id) ON DELETE SET NULL",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS annexure_ref VARCHAR(32)",
        "CREATE INDEX IF NOT EXISTS ix_cost_breakdown_lines_parent_boq_item_id ON cost_breakdown_lines(parent_boq_item_id)",
    ]
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            _log.warning(f"Schema drift fix skipped ({stmt!r}): {e}")


_apply_schema_drift_fixes()

# Seed system tools for agent builder
def _seed_system_tools():
    """Seed built-in system tools (document_reader, tender_lookup, etc.) if missing."""
    import time as _time
    from app.core.database import SessionLocal
    from app.services.agent_tools_service import seed_system_tools

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        db = SessionLocal()
        try:
            created = seed_system_tools(db)
            if created > 0:
                import logging
                logging.getLogger(__name__).info(f"Seeded {created} system tools into agent_tools table")
            return
        except Exception as e:
            db.rollback()
            if attempt < max_retries:
                import logging
                logging.getLogger(__name__).warning(
                    f"DB connection failed during tool seeding (attempt {attempt}/{max_retries}): {e}"
                )
                _time.sleep(2 * attempt)
            else:
                import logging
                logging.getLogger(__name__).error(f"Tool seeding failed after {max_retries} attempts: {e}")
        finally:
            db.close()

_seed_system_tools()

# Seed workspace document agents
def _seed_document_agents():
    """Seed specialized document agents for workspace (letter, technical, costing, compliance)."""
    from app.core.database import SessionLocal
    from app.services.seed_document_agents import seed_document_agents

    db = SessionLocal()
    try:
        created = seed_document_agents(db)
        if created > 0:
            import logging
            logging.getLogger(__name__).info(f"Seeded {created} workspace document agents")
    except Exception as e:
        db.rollback()
        import logging
        logging.getLogger(__name__).warning(f"Document agent seeding failed: {e}")
    finally:
        db.close()

_seed_document_agents()

# Seed published system agents (costing_researcher, tender_doc_analyzer)
# so the Admin → Agent Builder UI can edit their prompts. These rows are
# what `resolve_system_prompt` reads at runtime when an admin has customized
# the prompt — without the rows, edits in the UI would have nothing to save
# to and the runtime would silently keep using the canonical code constant.
# The seed scripts are idempotent: they refresh non-prompt metadata (tags,
# description, max_tokens) on every startup but only re-sync system_prompt
# + tools when `is_user_customized=False`, so admin edits survive restarts.
def _seed_system_agents():
    """Seed CustomAgent rows for published system agents on startup."""
    import logging as _logging
    _log = _logging.getLogger(__name__)
    from app.core.database import SessionLocal

    seeders = [
        ("costing_researcher", "app.services.seed_costing_researcher_agent", "seed_costing_researcher"),
        ("tender_doc_analyzer", "app.services.seed_tender_analyzer_agent", "seed_tender_analyzer"),
        ("general_assistant", "app.services.seed_general_assistant_agent", "seed_general_assistant_agent"),
    ]
    for agent_key, module_path, fn_name in seeders:
        db = SessionLocal()
        try:
            mod = __import__(module_path, fromlist=[fn_name])
            seed_fn = getattr(mod, fn_name)
            seed_fn(db=db)
        except Exception as e:
            db.rollback()
            _log.warning(
                f"System agent seeding failed for '{agent_key}': "
                f"{type(e).__name__}: {e}"
            )
        finally:
            db.close()

_seed_system_agents()


def _refresh_agent_models():
    """Move agent rows off superseded / invalid models onto their tier's current
    one. Runs after the system-agent seeders so freshly-seeded rows are covered
    in the same pass."""
    import logging as _logging
    _log = _logging.getLogger(__name__)
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        from app.services.seed_agent_models import seed_agent_models

        seed_agent_models(db)
    except Exception as e:
        db.rollback()
        _log.warning(
            f"Agent model refresh failed: {type(e).__name__}: {e}"
        )
    finally:
        db.close()


_refresh_agent_models()


def _backfill_agent_tools():
    """Free agents from canonical tool lists that now narrow them.

    An empty `tools` column means the whole shared repo, so a copied canonical
    list caps the agent instead of equipping it.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        from app.services.seed_agent_tools import seed_agent_tools

        seed_agent_tools(db)
    except Exception as e:
        db.rollback()
        _log.warning(f"Agent tool release failed: {type(e).__name__}: {e}")
    finally:
        db.close()


_backfill_agent_tools()

# Seed format templates for workspace
def _seed_format_templates():
    """Seed system format templates for common tender document types."""
    from app.core.database import SessionLocal
    from app.services.seed_format_templates import seed_format_templates

    db = SessionLocal()
    try:
        created = seed_format_templates(db)
        if created > 0:
            import logging
            logging.getLogger(__name__).info(f"Seeded {created} document format templates")
    except Exception as e:
        db.rollback()
        import logging
        logging.getLogger(__name__).warning(f"Format template seeding failed: {e}")
    finally:
        db.close()

_seed_format_templates()

# Migrate existing workspace documents: convert markdown-in-HTML to proper HTML
#: Marker recording that the workspace content migration has already run, in
#: the same shape as `agent_model_allocation_version`. Bump the version to make
#: it run again.
WORKSPACE_MIGRATION_SETTING_KEY = "workspace_content_migration_version"
WORKSPACE_MIGRATION_VERSION = "markdown-to-html-v1"


def _migrate_workspace_content():
    """One-time migration: convert markdown stored in draft_content_html to HTML.

    Gated on a marker because it had no memory of having run. Every boot it
    loaded every workspace row with content — 725 rows, 2.7 MB of HTML — to
    re-decide they were already fine, and `railway.json` starts uvicorn with
    `--workers 4`, so that happened four times per deploy before the service
    accepted traffic.

    The marker gates this startup call only. `migrate_markdown_content` itself
    is unchanged and still does the full scan whenever it is called directly —
    the admin route `POST /tenders/{id}/workspace/migrate-markdown` depends on
    that, and a migration you can no longer trigger on demand is worse than a
    noisy one.
    """
    import logging as _logging

    from app.core.database import SessionLocal
    from app.models.platform_setting import PlatformSetting
    from app.services.workspace_service import migrate_markdown_content

    _log = _logging.getLogger(__name__)
    db = SessionLocal()
    try:
        marker = db.query(PlatformSetting).filter(
            PlatformSetting.key == WORKSPACE_MIGRATION_SETTING_KEY
        ).first()
        if marker and marker.value == WORKSPACE_MIGRATION_VERSION:
            _log.info(
                "[DRPL] Workspace content migration: already applied (%s)",
                WORKSPACE_MIGRATION_VERSION,
            )
            return

        result = migrate_markdown_content(db)
        _log.info("[DRPL] Workspace content migration result: %s", result)

        if marker:
            marker.value = WORKSPACE_MIGRATION_VERSION
        else:
            db.add(PlatformSetting(
                key=WORKSPACE_MIGRATION_SETTING_KEY,
                value=WORKSPACE_MIGRATION_VERSION,
                value_type="string",
                category="system",
                description=(
                    "Internal marker for the workspace markdown-to-HTML content "
                    "migration. Updated automatically; bump to re-run it."
                ),
                is_secret=False,
            ))
        db.commit()
    except Exception as e:
        db.rollback()
        _log.warning(
            "[DRPL] Workspace content migration failed: %s: %s", type(e).__name__, e
        )
    finally:
        db.close()


_migrate_workspace_content()


# Backfill Command Center session titles from their linked tender's real title.
# Sessions often have generic AI-generated placeholders ("New Session",
# "Tender Analysis Assistance Needed") while the tender row already has the
# real title extracted from the uploaded documents. This migration syncs them.
def _backfill_session_titles_from_tenders():
    """Permanently fix Command Center session and tender titles using all available data.

    Checks in priority order: tender title → analysis summary → per-doc key_facts → filenames.
    Idempotent: only touches rows that still have generic/default titles.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    from app.core.database import SessionLocal
    from app.models.proposal import ProposalSession
    from app.models.tender import Tender, TenderDocument
    from app.models.document_analysis import TenderAnalysisSummary, DocumentExtractionResult
    from app.services.tender_enrichment_service import _is_default_title
    from app.api.routes.command_center import _best_session_title

    db = SessionLocal()
    try:
        sessions = (
            db.query(ProposalSession)
            .filter(
                ProposalSession.tender_id.isnot(None),
                ProposalSession.router_session_id.isnot(None),
            )
            .all()
        )
        tender_ids = list({s.tender_id for s in sessions})
        if not tender_ids:
            return

        tenders_by_id = {
            t.id: t
            for t in db.query(Tender).filter(Tender.id.in_(tender_ids)).all()
        }
        filenames_by_tender: dict = {}
        for tid, fname in db.query(TenderDocument.tender_id, TenderDocument.file_name).filter(
            TenderDocument.tender_id.in_(tender_ids)
        ).all():
            filenames_by_tender.setdefault(tid, []).append(fname or "")

        # Optional data sources — skip if schema drift makes them unreadable.
        analysis_by_tender: dict = {}
        try:
            for tid, summary in db.query(
                TenderAnalysisSummary.tender_id,
                TenderAnalysisSummary.requirement_summary,
            ).filter(
                TenderAnalysisSummary.tender_id.in_(tender_ids),
                TenderAnalysisSummary.requirement_summary.isnot(None),
            ).all():
                if summary:
                    analysis_by_tender[tid] = summary
        except Exception as e:
            db.rollback()
            _log.warning(f"backfill: skipping analysis summary lookup ({type(e).__name__}: {e})")

        per_doc_by_tender: dict = {}
        try:
            for tid, summary_json in db.query(
                DocumentExtractionResult.tender_id,
                DocumentExtractionResult.summary_json,
            ).filter(
                DocumentExtractionResult.tender_id.in_(tender_ids),
                DocumentExtractionResult.extraction_type == "per_doc_summary",
            ).all():
                if isinstance(summary_json, dict):
                    per_doc_by_tender.setdefault(tid, []).append(summary_json)
        except Exception as e:
            db.rollback()
            _log.warning(f"backfill: skipping per-doc facts lookup ({type(e).__name__}: {e})")

        sessions_updated = 0
        tenders_updated = 0

        for s in sessions:
            tender = tenders_by_id.get(s.tender_id)
            if not tender:
                continue
            best = _best_session_title(
                tender.title,
                analysis_by_tender.get(s.tender_id),
                per_doc_by_tender.get(s.tender_id, []),
                filenames_by_tender.get(s.tender_id, []),
            )
            if not best:
                continue
            if _is_default_title(s.title):
                s.title = best
                sessions_updated += 1
            if _is_default_title(tender.title):
                tender.title = best
                tenders_updated += 1

        if sessions_updated or tenders_updated:
            db.commit()
            _log.info(
                f"[DRPL] Backfilled titles: {sessions_updated} sessions, "
                f"{tenders_updated} tenders (scanned {len(sessions)} sessions)"
            )
        else:
            _log.info(
                f"[DRPL] Title backfill ran: 0 updates needed "
                f"(scanned {len(sessions)} sessions across {len(tenders_by_id)} tenders)"
            )
    except Exception as e:
        db.rollback()
        _log.warning(f"[DRPL] Session title backfill failed: {type(e).__name__}: {e}")
    finally:
        db.close()

_backfill_session_titles_from_tenders()


# Reap any AgentExecution rows that were left in status="running" by a
# previous process. These can only exist when a worker was killed
# (SIGKILL, container restart, deployment) before the in-process finally
# could mark the row terminal. Anything older than the RQ job timeout
# (30 min) definitely won't complete, so flip it to "cancelled" once at
# startup. Best-effort — runs synchronously, never blocks app boot.
def _reap_stuck_executions_on_startup():
    try:
        from app.services.agent_execution_finalizer import reap_stuck_executions
        reap_stuck_executions()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Startup reap of stuck AgentExecution rows failed: "
            f"{type(e).__name__}: {e}"
        )

_reap_stuck_executions_on_startup()


# The same treatment for AgentRun, which never had it. A killed worker leaves
# the row claiming "running" forever, and a browser reattaching to it waits on
# a Redis stream that will never emit `run_done` — a spinner that turns until
# the SSE endpoint's own 40-minute wall clock gives up. Reaping makes the row
# say what actually happened, and the stream ends on it.
def _reap_stuck_runs_on_startup():
    try:
        from app.services.run_service import reap_stuck_runs
        reap_stuck_runs()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Startup reap of stuck AgentRun rows failed: "
            f"{type(e).__name__}: {e}"
        )

_reap_stuck_runs_on_startup()


# Reap any Tender.eager_analysis_status left in "queued"/"running" by a
# previous process (worker killed mid-analysis via SIGKILL, container
# restart, deployment). Anything older than the cutoff is reset to null so
# the eager-analysis sweep picks the tender back up. Terminal states
# (done/failed) are untouched. Best-effort — runs synchronously, never
# blocks app boot.
def _reap_stuck_eager_analysis_on_startup():
    from app.core.database import SessionLocal
    from app.services.eager_analysis_service import reap_stuck_eager_analysis

    _log = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        n = reap_stuck_eager_analysis(db)
        _log.info(f"[DRPL] Startup reap of stuck eager-analysis markers: {n} reset")
    except Exception as e:
        _log.warning(
            f"Startup reap of stuck eager-analysis markers failed: "
            f"{type(e).__name__}: {e}"
        )
    finally:
        db.close()

# NOTE: this call is deferred until after _add_missing_columns() runs (below) —
# it queries Tender.eager_analysis_status/eager_analysis_at, which _add_missing_columns()
# is what backfills on an existing SQLite dev DB. Calling it here would log a
# spurious OperationalError on any DB that predates those columns.


# Seed notification preferences (one row per kind). Idempotent — only inserts
# kinds that don't already exist, so admin overrides via the UI are preserved.
def _seed_notification_preferences():
    from app.core.database import SessionLocal
    from app.services.seed_notification_preferences import seed_notification_preferences

    db = SessionLocal()
    try:
        seed_notification_preferences(db)
    except Exception as e:
        db.rollback()
        import logging
        logging.getLogger(__name__).warning(
            f"Notification preference seeding failed: {type(e).__name__}: {e}"
        )
    finally:
        db.close()

_seed_notification_preferences()


# Forward-migrate PlatformSetting rows whose value names a superseded model.
# A `PlatformSetting` beats the config default in `_get_effective_model`, so a
# stale row silently undoes a model change no matter what `config.py` says.
# This only ever ran via `seed_defaults`, which is called from `app/seed.py`
# and the admin-settings routes — never at boot. A live `ai_model` row on a
# superseded model therefore kept winning until a human happened to open the
# settings page, which is a coincidence rather than a migration: it is how the
# two-model rollout would have shipped with every unconfigured agent still on
# Sonnet. Only the forward migration runs here, not the full `seed_defaults` —
# rewriting known-stale values is idempotent and safe, while inserting every
# default row at boot is a larger decision.
def _forward_migrate_stale_settings():
    from app.core.database import SessionLocal
    from app.services.settings_service import _forward_migrate_known_stale

    db = SessionLocal()
    try:
        _forward_migrate_known_stale(db)
    except Exception as e:
        db.rollback()
        import logging
        logging.getLogger(__name__).warning(
            f"Stale settings migration failed: {type(e).__name__}: {e}"
        )
    finally:
        db.close()


_forward_migrate_stale_settings()


# Seed scheduled background jobs (daily digest, closing-date scan). Idempotent
# — relies on well-known job ids so a restart won't double-schedule.
def _seed_scheduled_jobs():
    try:
        from app.services.seed_scheduled_jobs import seed_scheduled_jobs
        seed_scheduled_jobs()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"Scheduled-job seeding failed: {type(e).__name__}: {e}"
        )

_seed_scheduled_jobs()


# Diagnose a missing LibreOffice at boot rather than as a mystery preview
# failure hours later.
def _probe_soffice():
    try:
        from app.services.xlsx_preview_service import soffice_available, SOFFICE_BIN

        if soffice_available():
            logging.getLogger(__name__).info(
                f"[startup] {SOFFICE_BIN} present — xlsx previews enabled"
            )
        else:
            logging.getLogger(__name__).warning(
                f"[startup] {SOFFICE_BIN} NOT found — xlsx previews will fail; "
                f"install libreoffice-calc in the image"
            )
    except Exception as e:
        logging.getLogger(__name__).warning(f"[startup] soffice probe failed: {e}")

_probe_soffice()


# Auto-add missing columns for SQLite (no Alembic migrations)
def _add_missing_columns():
    """Add columns that create_all won't add to existing tables."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    migrations = [
        ("cost_breakdowns", "created_by", "INTEGER"),
        ("api_usage_logs", "run_id", "VARCHAR(64)"),
        ("letterhead_templates", "letterhead_pdf_path", "TEXT"),
        ("digital_signatures", "default_position_x", "REAL"),
        ("digital_signatures", "default_position_y", "REAL"),
        ("custom_agents", "langchain_config", "TEXT"),
        ("custom_agents", "mcp_servers", "TEXT"),
        ("custom_agents", "learning_enabled", "BOOLEAN DEFAULT TRUE"),
        # Command Center extensions on proposal_sessions
        ("proposal_sessions", "router_session_id", "VARCHAR(100)"),
        ("proposal_sessions", "pipeline_state", "JSONB"),
        ("proposal_sessions", "mode", "VARCHAR(20) DEFAULT 'tender_linked'"),
        # Checklist automation fields
        ("checklist_items", "item_category", "VARCHAR(30) DEFAULT 'standard'"),
        ("checklist_items", "generation_status", "VARCHAR(30) DEFAULT 'pending'"),
        ("checklist_items", "generated_document_id", "INTEGER"),
        ("checklist_items", "generation_error", "TEXT"),
        ("checklist_items", "ai_instructions", "TEXT"),
        ("checklist_items", "source_section", "TEXT"),
        # Workspace fields
        ("checklist_items", "is_not_required", "BOOLEAN DEFAULT FALSE"),
        ("checklist_items", "workspace_status", "VARCHAR(30) DEFAULT 'not_started'"),
        ("checklist_items", "agent_key", "VARCHAR(100)"),
        ("tenders", "workspace_enabled", "BOOLEAN DEFAULT FALSE"),
        ("tenders", "location", "VARCHAR(255)"),
        ("tenders", "bid_type", "VARCHAR(50)"),
        ("tenders", "source_portal", "VARCHAR(50)"),
        ("tenders", "category", "VARCHAR(255)"),
        ("tenders", "below_threshold", "BOOLEAN DEFAULT FALSE"),
        ("tenders", "scoring_attempts", "INTEGER DEFAULT 0"),
        ("tenders", "segment", "VARCHAR(20)"),
        ("tenders", "segment_overridden", "BOOLEAN DEFAULT FALSE"),
        ("custom_agents", "document_categories", "TEXT DEFAULT '[]'"),
        # Unified template management columns
        ("proposal_templates", "output_format", "VARCHAR(10) DEFAULT 'docx'"),
        ("document_format_templates", "output_format", "VARCHAR(10) DEFAULT 'docx'"),
        ("document_format_templates", "original_file_path", "TEXT"),
        ("document_format_templates", "original_file_name", "VARCHAR(500)"),
        ("costing_templates", "output_format", "VARCHAR(10) DEFAULT 'xlsx'"),
        ("costing_templates", "original_file_name", "VARCHAR(500)"),
        # Annexure letterhead opt-out (SQLite counterpart of the Postgres drift fix).
        ("document_workspaces", "letterhead_disabled", "BOOLEAN DEFAULT FALSE"),
        # Linked document tracking
        ("tender_documents", "parent_document_id", "INTEGER"),
        ("tender_documents", "source_url", "TEXT"),
        ("tender_documents", "extraction_status", "VARCHAR(30) DEFAULT 'pending'"),
        ("chat_attachments", "parent_attachment_id", "INTEGER"),
        ("chat_attachments", "source_url", "TEXT"),
        ("chat_attachments", "extraction_status", "VARCHAR(30) DEFAULT 'pending'"),
        # Workflow builder integration on proposal_sessions
        ("proposal_sessions", "active_workflow_id", "INTEGER"),
        ("proposal_sessions", "workflow_execution_id", "INTEGER"),
        # Phase 7 follow-up — per-keyword pagination override for GeM auto-search
        ("tender_scope_profiles", "max_pages_per_keyword", "INTEGER NOT NULL DEFAULT 5"),
        # NIT-aware costing — see plan: now-i-need-to-synchronous-taco.md
        ("boq_items", "item_code", "VARCHAR(64)"),
        ("boq_items", "schedule_name", "VARCHAR(64)"),
        ("boq_items", "bidding_unit", "VARCHAR(64)"),
        ("boq_items", "basic_value", "REAL"),
        ("boq_items", "escalation_pct", "REAL"),
        ("boq_items", "is_tax_line", "BOOLEAN NOT NULL DEFAULT 0"),
        ("boq_items", "extraction_confidence", "VARCHAR(16)"),
        ("cost_breakdown_lines", "boq_item_id", "INTEGER"),
        ("cost_breakdown_lines", "item_code", "VARCHAR(64)"),
        ("cost_breakdown_lines", "schedule_name", "VARCHAR(64)"),
        ("cost_breakdown_lines", "bidding_unit", "VARCHAR(64)"),
        ("cost_breakdown_lines", "basic_value", "REAL"),
        ("cost_breakdown_lines", "escalation_pct", "REAL"),
        ("cost_breakdown_lines", "is_tax_line", "BOOLEAN NOT NULL DEFAULT 0"),
        ("cost_breakdown_lines", "annexure", "VARCHAR(8)"),
        ("cost_breakdown_lines", "oem_manufacturer", "VARCHAR(128)"),
        ("cost_breakdown_lines", "source_url", "TEXT"),
        ("cost_breakdowns", "cost_sheet_template", "VARCHAR(32)"),
        # Offline document signing — original (clean) uploaded PDF for re-sign.
        ("generated_documents", "source_file_path", "TEXT"),
        # Reconciliation gate — sum(line amounts) vs the tender's own stated
        # schedule totals (BOQScheduleTotal). Flag only, never mutates lines.
        ("cost_breakdowns", "reconciliation_json", "TEXT"),
        ("cost_breakdowns", "needs_review", "BOOLEAN NOT NULL DEFAULT 0"),
        # Eager analysis (pre-fetch pipeline warmup) — per-tender in-flight state
        ("tenders", "eager_analysis_status", "VARCHAR(16)"),
        ("tenders", "eager_analysis_at", "TIMESTAMP"),
        ("tenders", "archived_at", "TIMESTAMP"),
        ("tenders", "archive_reason", "VARCHAR(30)"),
        ("boq_schedule_totals", "reconciliation_status", "VARCHAR(16)"),
        # The schedule banner as printed (see BOQScheduleTotal.title).
        ("boq_schedule_totals", "title", "TEXT"),
        # Streamed-so-far output of a run (see AgentRun.partial_output).
        ("agent_runs", "partial_output", "TEXT"),
        ("agent_runs", "partial_trace", "JSON"),
        # Annexure components (see BOQItem.parent_item_id).
        ("boq_items", "annexure_ref", "VARCHAR(32)"),
        ("boq_items", "parent_item_id", "INTEGER"),
        ("boq_items", "source_document_id", "INTEGER"),
        ("cost_breakdown_lines", "parent_boq_item_id", "INTEGER"),
        ("cost_breakdown_lines", "annexure_ref", "VARCHAR(32)"),
    ]
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            if table in inspector.get_table_names():
                existing = [c["name"] for c in inspector.get_columns(table)]
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                    conn.commit()

        # Set output_format='xlsx' for BOQ document format templates
        if "document_format_templates" in inspector.get_table_names():
            try:
                conn.execute(text(
                    "UPDATE document_format_templates SET output_format = 'xlsx' "
                    "WHERE document_category = 'boq' AND (output_format IS NULL OR output_format = 'docx')"
                ))
                conn.commit()
            except Exception:
                pass

        # Raw-markdown-only tender analysis: drop the duplicated structured JSON column
        # (SQLite ≥ 3.35 supports DROP COLUMN; skip silently on older builds).
        if "tender_analysis_summaries" in inspector.get_table_names():
            existing = [c["name"] for c in inspector.get_columns("tender_analysis_summaries")]
            if "structured_analysis" in existing:
                try:
                    conn.execute(text(
                        "ALTER TABLE tender_analysis_summaries DROP COLUMN structured_analysis"
                    ))
                    conn.commit()
                except Exception:
                    pass

        # Fix: pipeline_state was initially added as TEXT, needs to be JSONB for PostgreSQL
        if "proposal_sessions" in inspector.get_table_names():
            for col in inspector.get_columns("proposal_sessions"):
                if col["name"] == "pipeline_state" and str(col["type"]) == "TEXT":
                    conn.execute(text(
                        "ALTER TABLE proposal_sessions "
                        "ALTER COLUMN pipeline_state TYPE JSONB USING pipeline_state::jsonb"
                    ))
                    conn.commit()
                    break

_add_missing_columns()

# Runs after _add_missing_columns() (SQLite self-heal) so eager_analysis_status/
# eager_analysis_at columns exist before this query runs — see NOTE above.
_reap_stuck_eager_analysis_on_startup()

from app.core.config import platform_build
logging.getLogger("drpl.main").info("DRPL web starting: build %s", platform_build())

app = FastAPI(
    title="DRPL Tender Intelligence Platform",
    description="Backend API for the DRPL AI-Powered Tender Intelligence Platform",
    version="3.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# CORS — configurable via CORS_ORIGINS env var (comma-separated)
import os as _os
_cors_origins_str = _os.environ.get("CORS_ORIGINS", "*")
_cors_origins = (
    [o.strip() for o in _cors_origins_str.split(",") if o.strip()]
    if _cors_origins_str != "*"
    else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],  # Enable credentials when origins are explicit
    allow_methods=["*"],
    allow_headers=["*"],
)

# Core routes
app.include_router(auth.router, prefix="/api")
app.include_router(extension.router, prefix="/api")
app.include_router(tenders.router, prefix="/api")
app.include_router(monitoring.router, prefix="/api")
app.include_router(checklist.router, prefix="/api")
app.include_router(proposals.router, prefix="/api")

# Admin routes (master_admin only)
app.include_router(admin_settings.router, prefix="/api")
app.include_router(admin_scope_profile.router, prefix="/api")
app.include_router(admin_users.router, prefix="/api")
# admin_agents router removed — all AI agent config now managed via agent_builder
app.include_router(admin_security.router, prefix="/api")
app.include_router(admin_audit.router, prefix="/api")
app.include_router(admin_dashboard.router, prefix="/api")
app.include_router(usage.router, prefix="/api")
app.include_router(usage.admin_router, prefix="/api")
app.include_router(tender_scoring_admin.router, prefix="/api")
app.include_router(training_datasets.router, prefix="/api")
app.include_router(ratecards.router, prefix="/api")
app.include_router(templates.router, prefix="/api")

# Document analysis routes
app.include_router(document_analysis.router, prefix="/api")
app.include_router(document_analysis.batch_router, prefix="/api")

# Letterhead, signatures, and document generation routes
app.include_router(letterhead.router, prefix="/api")
app.include_router(signatures.router, prefix="/api")
app.include_router(documents.router, prefix="/api")
app.include_router(offline_documents.router, prefix="/api")

# Agent builder routes
app.include_router(agent_builder.router, prefix="/api")

# Cost breakdown editor (per-tender editable cost sheet)
app.include_router(cost_breakdown.router, prefix="/api")
app.include_router(cost_breakdowns.router, prefix="/api")

# LangChain agent routes (pipeline, chat, memory, MCP)
app.include_router(langchain_agents.router, prefix="/api")

# Batch processing routes (Claude Message Batches API)
app.include_router(batch_processing.router, prefix="/api")

# Context management routes (token counting, caching, compaction)
app.include_router(context_management.router, prefix="/api")
app.include_router(command_center.router, prefix="/api")

# Workspace routes (per-tender canvas workspace)
app.include_router(workspace.router, prefix="/api")

# Workflow builder routes (visual agentic workflow builder)
app.include_router(workflows.router, prefix="/api")

# Agent Runs (Phase C: RQ-backed queue + pub/sub SSE)
app.include_router(runs.router, prefix="/api")
app.include_router(collect.router, prefix="/api")

# Notification Center (in-app + email via Resend)
app.include_router(notifications.router)
app.include_router(admin_notifications.router)


@app.get("/")
def root():
    return {"name": "DRPL Tender Intelligence Platform", "version": "3.0.0", "status": "running"}


@app.get("/health")
def health():
    """Liveness, plus the commit this process is actually serving.

    A deploy that did not land is otherwise invisible from outside: the schema
    only moves when a request or response model changes, so a fix to a service
    function looks byte-identical from `/openapi.json` and "is my change live?"
    becomes guesswork. `platform_build` reads Railway's own
    RAILWAY_GIT_COMMIT_SHA, so this is the deployed commit rather than
    something the code asserts about itself.
    """
    return {"status": "healthy", "build": platform_build()}


@app.get("/health/capacity")
def health_capacity():
    """Observability snapshot — queue depth, active runs, DB pool, recent 429s.

    Safe to call without auth (returns only counters, no user data). Used by
    scripts/load_test_command_center.py and internal dashboards.
    """
    from app.core.llm_metrics import count_recent_rate_limits
    from app.core.redis_client import get_redis, get_queue

    # DB pool (web process view)
    pool = engine.pool
    pool_info = {}
    try:
        pool_info = {
            "checked_out": pool.checkedout(),
            "size": pool.size(),
            "overflow": pool.overflow(),
        }
    except Exception:
        pass

    # Redis/RQ
    v2 = {
        "queue_depth": 0,
        "active_runs_global": 0,
        "active_runs_by_worker": {},
        "oldest_queued_age_seconds": None,
        "llm_429_last_5m": 0,
    }
    q = get_queue()
    if q is not None:
        try:
            v2["queue_depth"] = int(q.count)
        except Exception:
            pass

    client = get_redis()
    if client is not None:
        # Global active runs (DB-backed — cheap query)
        try:
            from app.core.database import SessionLocal
            from app.models.agent_run import AgentRun
            from sqlalchemy import func
            db = SessionLocal()
            try:
                active = (
                    db.query(func.count(AgentRun.id))
                    .filter(AgentRun.status.in_(["queued", "running"]))
                    .scalar()
                )
                v2["active_runs_global"] = int(active or 0)

                oldest = (
                    db.query(func.min(AgentRun.created_at))
                    .filter(AgentRun.status == "queued")
                    .scalar()
                )
                if oldest is not None:
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)
                    # `oldest` may be naive if DB returns naive timestamps — treat as UTC.
                    if oldest.tzinfo is None:
                        oldest = oldest.replace(tzinfo=timezone.utc)
                    v2["oldest_queued_age_seconds"] = int((now - oldest).total_seconds())
            finally:
                db.close()
        except Exception:
            pass

        try:
            v2["llm_429_last_5m"] = count_recent_rate_limits(minutes=5)
        except Exception:
            pass

    # The worker's own build, as the worker last reported it on boot.
    #
    # This is the number that has actually cost time: the worker is the process
    # that reads the NIT and prices the schedule, so a build that reached the
    # web service and not the worker reads as "the fix changed nothing". The
    # web tier cannot know it first-hand, so the worker writes it to Redis at
    # startup (`worker.py`) and this repeats it. Absent means no worker has
    # booted since the key expired, which is itself worth seeing.
    workers: dict[str, str] = {}
    conn = None
    try:
        from app.core.redis_client import get_redis as _r
        conn = _r()
        if conn is not None:
            raw = conn.hgetall("drpl:worker:build")
            workers = {
                (k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v)
                for k, v in (raw or {}).items()
            }
    except Exception:
        pass

    # Prune the dead. The hash is written once per child at boot and never
    # refreshed, so a replaced container leaves its fields behind for the
    # hash's whole TTL -- the first deploy after this endpoint shipped
    # reported 24 children on two different builds, twelve of them gone. RQ
    # already heartbeats its own worker registry, so intersecting with it is
    # the liveness signal, and no second heartbeat has to be invented.
    #
    # If the intersection comes back empty while the hash is not, the registry
    # is the thing that is wrong (a momentary read, a naming change) -- so the
    # unfiltered map is reported rather than claiming no workers exist. Losing
    # a worker from this list is worse than showing one too many.
    try:
        if workers and conn is not None:
            from rq import Worker as _RQWorker

            live = set()
            for w in _RQWorker.all(connection=conn):
                host = w.hostname.decode() if isinstance(w.hostname, bytes) else w.hostname
                live.add(f"{host}:{w.pid}")
            pruned = {k: v for k, v in workers.items() if k in live}
            if pruned:
                workers = pruned
    except Exception:
        pass

    # The one-glance answer: how many live children on each build. Two entries
    # means a rollout in flight, or a replica stuck behind -- which is the
    # thing worth noticing and the reason this is a tally and not a total.
    worker_builds: dict[str, int] = {}
    for sha in workers.values():
        worker_builds[sha] = worker_builds.get(sha, 0) + 1

    return {
        "build": platform_build(),
        "worker_builds": worker_builds,
        "workers": workers,
        "web_db_pool": pool_info,
        "v2": v2,
    }


@app.get("/health/costing")
def health_costing(window_minutes: int = 60):
    """Costing-agent reliability snapshot — pre-flight trims, overflows after
    trim, persistence success vs failure across the last `window_minutes`.

    Added by the costing-reliability work (plan:
    now-i-need-to-synchronous-taco.md) so we can verify the no-lies-artifacts +
    pre-flight-budget fixes are actually working in production, and so
    regressions surface in one obvious place. Safe to call without auth —
    returns only counters, no user data.
    """
    from app.core.costing_metrics import get_costing_health

    # Clamp to a reasonable window so callers can't ask for a year.
    minutes = max(1, min(int(window_minutes), 24 * 60))
    return get_costing_health(minutes=minutes)
