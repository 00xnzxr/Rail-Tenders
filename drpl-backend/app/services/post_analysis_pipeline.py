"""
Post-analysis fan-out pipeline.

Runs three agents together after a tender's first-level analysis completes:
  1. annexure_finder   — Claude Vision over the tender PDFs, materializes every
                         annexure/schedule/proforma as a paired ChecklistItem +
                         DocumentWorkspace row.
  2. checklist_generator — text-prompt pass that produces the submission checklist.
  3. workspace_initializer — creates any missing DocumentWorkspace rows for
                             ChecklistItems that don't yet have one.

(1) and (2) run concurrently via asyncio.gather, each on its own SQLAlchemy
session (SA sessions are not thread/coroutine-safe). (3) runs on the caller's
session after both finish so it can see their committed writes.

Idempotent — safe to re-run. Annexure rows are matched by
ChecklistItem.source_section == "annexure_finder:{identifier}"; locked
workspaces (review_status in in_review/approved) are preserved verbatim.
"""

import asyncio
import logging
from contextlib import contextmanager
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@contextmanager
def _fresh_session():
    """Yield an independent SQLAlchemy session. Closed (not committed) on exit."""
    from app.core.database import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        try:
            s.close()
        except Exception:
            pass


async def _run_annexures(tender_id: int) -> dict:
    from app.services.langchain.graphs.annexure_finder_agent import (
        run_annexure_extraction,
    )
    with _fresh_session() as s:
        try:
            return await run_annexure_extraction(s, tender_id)
        except Exception as e:
            logger.exception(
                f"[pipeline] annexure_finder failed for tender {tender_id}: {e}"
            )
            try:
                s.rollback()
            except Exception:
                pass
            return {"status": "failed", "error": str(e)}


async def _run_checklist(tender_id: int) -> dict:
    from app.services.checklist_service import generate_checklist
    with _fresh_session() as s:
        try:
            items = await generate_checklist(s, tender_id)
            return {"status": "completed", "count": len(items)}
        except Exception as e:
            logger.exception(
                f"[pipeline] checklist_generator failed for tender {tender_id}: {e}"
            )
            try:
                s.rollback()
            except Exception:
                pass
            return {"status": "failed", "error": str(e)}


async def run_post_analysis_pipeline(
    db: Session,
    tender_id: int,
    user_id: Optional[int] = None,
) -> dict:
    """Fan-out orchestrator: annexures + checklist in parallel → workspace init.

    Args:
        db: Primary SQLAlchemy session (used for workspace init after the
            parallel tasks commit on their own sessions).
        tender_id: Tender row id.
        user_id: Authenticated user id (falls back to tender.assigned_user_id
            or 0 if unknown — workspace init just stamps it into counters).

    Returns:
        {
          "annexure_finder":    <run_annexure_extraction summary>,
          "checklist_generator": {"status": "...", "count": n},
          "workspace":          <workspace overview dict or {"status":"failed"...}>,
        }
    """
    logger.info(f"[pipeline] starting for tender {tender_id}")

    annex_task = asyncio.create_task(_run_annexures(tender_id))
    cl_task = asyncio.create_task(_run_checklist(tender_id))
    annex_res, cl_res = await asyncio.gather(annex_task, cl_task)

    logger.info(
        f"[pipeline] tender={tender_id} annexures={annex_res.get('status')} "
        f"checklist={cl_res.get('status')}"
    )

    # Workspace init runs on the caller's session after both writes commit.
    from app.services.workspace_service import init_workspace
    ws_res: dict
    try:
        db.expire_all()  # refresh: pick up rows committed by the fresh sessions
        ws_res = init_workspace(db, tender_id, user_id or 0)
        logger.info(f"[pipeline] tender={tender_id} workspace=completed")
    except Exception as e:
        logger.exception(
            f"[pipeline] init_workspace failed for tender {tender_id}: {e}"
        )
        try:
            db.rollback()
        except Exception:
            pass
        ws_res = {"status": "failed", "error": str(e)}

    return {
        "annexure_finder": annex_res,
        "checklist_generator": cl_res,
        "workspace": ws_res,
    }


def run_pipeline_in_background(tender_id: int, user_id: Optional[int] = None) -> None:
    """Sync wrapper for FastAPI BackgroundTasks.

    Opens a fresh session, runs the pipeline to completion, closes. Never
    raises — exceptions are logged and swallowed so a failed background task
    can't propagate into the request cycle.
    """
    try:
        with _fresh_session() as s:
            try:
                asyncio.run(run_post_analysis_pipeline(s, tender_id, user_id))
            except RuntimeError:
                # Already in an event loop (shouldn't happen in BackgroundTasks,
                # but guard anyway).
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        run_post_analysis_pipeline(s, tender_id, user_id)
                    )
                finally:
                    loop.close()
    except Exception as e:
        logger.exception(
            f"[pipeline] background run failed for tender {tender_id}: {e}"
        )
