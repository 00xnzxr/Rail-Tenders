"""
Costing PDF diagnostic — token-free.

Run: python -m scripts.diagnose_costing_pdf <tender_id>

Reports the full state required by the costing agent's native-PDF attach
path: TenderDocument rows, BOQItem rows, latest cost_breakdowns row,
relevant PlatformSettings, the resolved model + provider, and a dry-run
of `_build_tender_pdf_content_blocks` showing exactly which gate would
admit or skip each PDF.

Makes ZERO Anthropic API calls. Use this BEFORE re-running an actual
costing test to confirm the code path will fire.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Optional


def _hr(title: str = "") -> None:
    bar = "=" * 70
    if title:
        print(f"\n{bar}\n{title}\n{bar}")
    else:
        print(bar)


def _fmt_size(n: int) -> str:
    if n is None:
        return "?"
    if n > 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n > 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _truncate(s: Optional[str], n: int = 120) -> str:
    if not s:
        return "<empty>"
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."


def main(tender_id: int) -> int:
    from app.core.database import SessionLocal
    from app.models.tender import Tender, TenderDocument
    from app.models.cost_breakdown import CostBreakdown
    from app.models.costing_template import BOQItem
    from app.services.ai_service import (
        _get_agent_config,
        _get_effective_model,
        _build_pdf_content_blocks,
        _get_pdf_page_count,
    )
    from app.services.langchain.provider_config import detect_provider
    from app.services.settings_service import get_effective_setting
    from app.services.storage_service import get_storage_service
    import contextlib

    db = SessionLocal()
    try:
        # --- Tender ----------------------------------------------------
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        _hr(f"Tender {tender_id}")
        if not tender:
            print(f"ERROR: no Tender row with id={tender_id}")
            return 2
        print(f"  title         : {_truncate(tender.title, 100)}")
        print(f"  organisation  : {tender.organisation}")
        print(f"  portal        : {tender.portal}")
        print(f"  tender_id_ext : {tender.tender_id}")
        print(f"  status        : {tender.status}")

        # --- Documents -------------------------------------------------
        docs = (
            db.query(TenderDocument)
            .filter(TenderDocument.tender_id == tender_id)
            .order_by(TenderDocument.id.asc())
            .all()
        )
        _hr(f"TenderDocument rows ({len(docs)})")
        if not docs:
            print("  (none)")
        for d in docs:
            print(
                f"  id={d.id}  mime={d.mime_type}  size={_fmt_size(d.file_size)}  "
                f"name={_truncate(d.file_name, 60)}"
            )
            print(f"     file_path: {d.file_path}")

        pdf_docs = [d for d in docs if d.mime_type == "application/pdf"]

        # --- BOQ items -------------------------------------------------
        boq_rows = (
            db.query(BOQItem)
            .filter(BOQItem.tender_id == tender_id)
            .order_by(BOQItem.sr_no.asc())
            .all()
        )
        _hr(f"BOQItem rows ({len(boq_rows)})")
        if boq_rows:
            print(
                "  Note: non-empty boq_items means the costing agent enters "
                "the `if boq_items:` branch.\n"
                "        v2 attaches native PDFs there too (v1 didn't)."
            )
            for b in boq_rows[:5]:
                print(
                    f"  sr_no={b.sr_no}  qty={b.quantity} {b.unit or ''}  "
                    f"est_rate={b.estimated_rate}\n"
                    f"     desc: {_truncate(b.description, 100)}"
                )
            if len(boq_rows) > 5:
                print(f"  ... and {len(boq_rows) - 5} more")
        else:
            print("  (none) — costing agent enters the no-BOQ `else:` branch.")

        # --- Latest cost_breakdown ------------------------------------
        latest_cb = (
            db.query(CostBreakdown)
            .filter(CostBreakdown.tender_id == tender_id)
            .order_by(CostBreakdown.version.desc())
            .first()
        )
        _hr("Latest cost_breakdowns row")
        if not latest_cb:
            print("  (none) — no costing has been run yet for this tender.")
        else:
            print(
                f"  id={latest_cb.id}  version={latest_cb.version}  "
                f"status={latest_cb.status}  needs_input_count={latest_cb.needs_input_count}"
            )
            print(f"  grand_total={latest_cb.grand_total}")
            recs = latest_cb.recommendations_json or []
            if recs:
                print(f"  recommendations[0]: {_truncate(recs[0], 200)}")
            assumptions = latest_cb.assumptions_json or []
            if assumptions:
                print(f"  assumptions[0]:     {_truncate(assumptions[0], 200)}")

        # --- Relevant settings ----------------------------------------
        _hr("PlatformSettings")
        keys = [
            ("claude_pdf_native_enabled", True),
            ("claude_pdf_max_pages", 100),
            ("claude_pdf_cache_enabled", True),
            ("costing_thinking_enabled", False),
        ]
        for k, default in keys:
            v = get_effective_setting(db, k, default)
            print(f"  {k:30s} = {v!r:>12}  (default {default!r})")

        # --- Agent config & provider ----------------------------------
        agent_cfg = _get_agent_config(db, "costing_researcher") or {}
        model_name = _get_effective_model(db, agent_cfg)
        provider = detect_provider(model_name)
        _hr("costing_researcher resolution")
        print(f"  agent_config.model : {agent_cfg.get('model')}")
        print(f"  effective model    : {model_name}")
        print(f"  detected provider  : {provider}")
        if provider != "anthropic":
            print(
                f"  WARNING: native PDF attach only fires when provider == "
                f"'anthropic'. Current = {provider!r}."
            )

        # --- Helper dry-run -------------------------------------------
        _hr("_build_tender_pdf_content_blocks dry-run")
        native_enabled = get_effective_setting(db, "claude_pdf_native_enabled", True)
        if isinstance(native_enabled, str):
            native_enabled = native_enabled.lower() in ("true", "1", "yes")
        max_pages = int(get_effective_setting(db, "claude_pdf_max_pages", 100) or 100)
        cache_enabled = get_effective_setting(db, "claude_pdf_cache_enabled", True)
        if isinstance(cache_enabled, str):
            cache_enabled = cache_enabled.lower() in ("true", "1", "yes")

        print(f"  native_enabled     : {native_enabled}")
        print(f"  max_pages          : {max_pages}")
        print(f"  cache_enabled      : {cache_enabled}")

        if not native_enabled:
            print("\n  HARD SKIP: claude_pdf_native_enabled=False → 0 blocks.")
            print("  Verdict: native PDF will NOT be attached on next run.")
            return 0
        if not pdf_docs:
            print("\n  HARD SKIP: no application/pdf TenderDocument rows.")
            print("  Verdict: native PDF will NOT be attached on next run.")
            return 0

        storage = get_storage_service()
        block_count = 0
        skipped = []
        with contextlib.ExitStack() as stack:
            for doc in pdf_docs[:3]:  # max_docs=3 in helper
                key = doc.file_path
                if not key:
                    print(f"  doc {doc.id}: empty file_path — SKIP")
                    skipped.append(f"doc {doc.id}: empty path")
                    continue
                try:
                    local_path = stack.enter_context(
                        storage.as_local_file(key, suffix=".pdf")
                    )
                except FileNotFoundError:
                    print(f"  doc {doc.id}: storage miss (key={key}) — SKIP")
                    skipped.append(f"doc {doc.id}: storage miss")
                    continue
                except Exception as e:
                    print(f"  doc {doc.id}: materialise error: {e} — SKIP")
                    skipped.append(f"doc {doc.id}: materialise error")
                    continue
                try:
                    file_size = os.path.getsize(local_path)
                    page_count = _get_pdf_page_count(local_path)
                    too_big = file_size > 32 * 1024 * 1024
                    too_many_pages = page_count > max_pages
                    print(
                        f"  doc {doc.id}: file={_fmt_size(file_size)}  "
                        f"pages={page_count}  "
                        f"32MB_check={'FAIL' if too_big else 'OK'}  "
                        f"max_pages_check={'FAIL' if too_many_pages else 'OK'}"
                    )
                    if too_big:
                        skipped.append(f"doc {doc.id}: >32 MB")
                        continue
                    if too_many_pages:
                        print(
                            f"     -> would skip ({page_count} > {max_pages}). "
                            f"Raise `claude_pdf_max_pages` to allow."
                        )
                        skipped.append(
                            f"doc {doc.id}: {page_count}p > {max_pages}"
                        )
                        continue
                    # Don't actually keep the b64 — just confirm it can build.
                    _build_pdf_content_blocks(local_path, cache_enabled)
                    block_count += 1
                    print(f"     -> would build block ✓")
                except Exception as e:
                    print(f"  doc {doc.id}: build error: {e} — SKIP")
                    skipped.append(f"doc {doc.id}: build error")
                    continue

        # --- Verdict ---------------------------------------------------
        _hr("Verdict")
        if block_count > 0:
            print(
                f"  ✓ Native PDF WILL fire on next costing run "
                f"({block_count} block(s) attached)."
            )
            if boq_rows:
                print(
                    f"  ✓ boq_items={len(boq_rows)} present → BOQ-populated "
                    f"branch (v2 still attaches PDFs)."
                )
            else:
                print(
                    f"  ✓ boq_items=0 → no-BOQ branch (PDFs are the agent's "
                    f"only scope source)."
                )
            print(
                f"\n  Next: restart the backend and run ONE costing test. "
                f"Watch logs for:"
            )
            print(
                f"    [costing pdf] tender {tender_id}: native PDF attach -> "
                f"{block_count} block(s)"
            )
            print(
                f"    [costing pdf] tender {tender_id}: total {block_count} "
                f"block(s) attached"
            )
        else:
            print(f"  ✗ Native PDF will NOT attach. Skipped: {skipped}")
            print(
                f"  Fix the skip reason above before spending tokens on "
                f"another costing test."
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.diagnose_costing_pdf <tender_id>")
        sys.exit(1)
    try:
        sys.exit(main(int(sys.argv[1])))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(3)
