# Command Center Session Navigation — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose why the existing tender-title enrichment pipeline doesn't always produce good `ProposalSession.title` values in practice, and strengthen the multi-document extraction so the master RFP wins regardless of upload order.

**Architecture:** The underlying enrichment is already implemented in `drpl-backend/app/services/tender_enrichment_service.py`. Phase 3 changes are surgical: (1) replace the first-non-empty primary-doc heuristic with a scored picker that prefers docs richer in tender-identification signals, (2) add per-blob fallback lookups for `tender_reference` / `scope_summary` when the picked primary doc is missing them, (3) add diagnostic logs at each enrichment branch so silent failures are visible in the costing-log file, (4) add a confirmatory log line in the startup backfill that fires regardless of whether updates happened.

**Tech Stack:** Python 3 + SQLAlchemy. No new dependencies. Backend-only change. No frontend impact.

**Important context for engineer:**
- This phase modifies existing code; no new files are created.
- The frontend is unchanged.
- `tender_enrichment_service.enrich_tender_from_analysis` already runs from `document_analysis_agent._run_v2_analysis` after the analyzer completes (around line 1814 of `document_analysis_agent.py`). Verify in logs that it fires by tailing `drpl-backend/logs/costing_run.log` after an analyzer run.
- The startup backfill `_backfill_session_titles_from_tenders()` in `drpl-backend/app/main.py` runs on every server start.
- The user works in place on `main`.
- Spec: `docs/superpowers/specs/2026-05-18-command-center-session-navigation-design.md` (Phase 3 section).
- The backend test framework is `pytest`, but there are no tests for `tender_enrichment_service` today. Don't add a test framework module — verification is done by reading logs from a real analyzer run.

---

## File Structure

**No new files.** Phase 3 modifies two existing files:

- `drpl-backend/app/services/tender_enrichment_service.py` — replace the first-non-empty primary-doc selection with a scored picker + per-blob fallback + diagnostic logs
- `drpl-backend/app/main.py` — add a single confirmatory log line in `_backfill_session_titles_from_tenders`

---

## Task 1: Stronger primary-document picker + per-blob fallbacks

The current picker (line 133-140 of `tender_enrichment_service.py`) takes the first per-doc blob with any non-empty `key_facts`. On multi-doc tenders where the master RFP isn't the first uploaded, the picker grabs an addendum / corrigendum / annexure as "primary" and misses the real tender reference and scope.

**Files:**
- Modify: `drpl-backend/app/services/tender_enrichment_service.py`

- [ ] **Step 1: Add the scoring + picker helpers**

Open `drpl-backend/app/services/tender_enrichment_service.py`. Find the existing helper section (somewhere around line 70-100, right above the `enrich_tender_from_analysis` function). Add these two functions just BEFORE `enrich_tender_from_analysis`:

```python
def _score_facts_richness(facts: dict) -> int:
    """Score a per-doc key_facts blob by how 'primary-document'-like it is.

    Higher = more likely to be the master RFP / NIT (which carries the
    tender reference and the canonical scope description). Annexures,
    addenda, and supplementary docs typically score low.

    Signals (cumulative):
      +5  has a non-empty `tender_reference`
      +3  has a non-empty `scope_summary`
      +1  has a non-empty `issuing_authority`
      +1  scope_summary is reasonably long (>= 80 chars)
      +2  has a non-empty `estimated_value`
    """
    if not isinstance(facts, dict):
        return 0
    score = 0
    if (facts.get("tender_reference") or "").strip():
        score += 5
    scope = (facts.get("scope_summary") or "").strip()
    if scope:
        score += 3
        if len(scope) >= 80:
            score += 1
    if (facts.get("issuing_authority") or "").strip():
        score += 1
    if (facts.get("estimated_value") or "").strip():
        score += 2
    return score


def _pick_primary_facts(per_doc_blobs: list[dict]) -> tuple[dict, str]:
    """Pick the most-authoritative per-doc key_facts blob.

    Returns ``(primary_facts, doc_name)``. ``doc_name`` is empty when the
    picker couldn't identify a source doc (e.g. the blob has no doc_name).
    Falls back to the first blob with any non-empty key_facts when all
    scores tie at 0 — preserving legacy behavior so we don't regress on
    single-doc tenders.

    For multi-doc tenders this picks the blob whose key_facts looks most
    like a master RFP (rich set of fields, long scope_summary, has a
    real tender_reference). That's the master NIT bundle most of the time.
    """
    best_score = -1
    best_facts: dict = {}
    best_doc_name: str = ""

    for blob in per_doc_blobs:
        if not isinstance(blob, dict):
            continue
        kf = blob.get("key_facts")
        if not isinstance(kf, dict) or not any(kf.values()):
            continue
        score = _score_facts_richness(kf)
        if score > best_score:
            best_score = score
            best_facts = kf
            best_doc_name = blob.get("doc_name") or ""

    # Legacy fallback: if NO blob scored above 0 (e.g. all key_facts have
    # only obscure fields), use the first non-empty blob.
    if best_score <= 0:
        for blob in per_doc_blobs:
            if not isinstance(blob, dict):
                continue
            kf = blob.get("key_facts")
            if isinstance(kf, dict) and any(kf.values()):
                return kf, blob.get("doc_name") or ""

    return best_facts, best_doc_name


def _scan_all_blobs_for(per_doc_blobs: list[dict], field: str) -> Optional[str]:
    """Cross-doc fallback: return the first non-empty value of ``field``
    found in any blob's ``key_facts``. Used when the picked primary doc
    is missing a field (e.g. picker chose a doc with a strong scope but
    the tender_reference lives on the addendum)."""
    for blob in per_doc_blobs:
        if not isinstance(blob, dict):
            continue
        kf = blob.get("key_facts")
        if not isinstance(kf, dict):
            continue
        v = kf.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None
```

These helpers don't reference anything outside themselves — they take in dicts and return scored picks. The `Optional` type hint requires an existing `from typing import Optional` import; check the top of the file and add it if absent.

- [ ] **Step 2: Wire the new picker into enrich_tender_from_analysis**

Find this block in `enrich_tender_from_analysis` (currently around line 133-140):

```python
        per_doc_facts = _collect_per_doc_key_facts(db, tender_id)
        # First per-doc with non-empty key_facts (usually the main RFP/NIT)
        primary_facts: dict = {}
        for blob in per_doc_facts:
            kf = blob.get("key_facts") if isinstance(blob, dict) else None
            if isinstance(kf, dict) and any(kf.values()):
                primary_facts = kf
                break
```

Replace with:

```python
        per_doc_facts = _collect_per_doc_key_facts(db, tender_id)
        primary_facts, primary_doc_name = _pick_primary_facts(per_doc_facts)
        logger.info(
            f"[enrich] tender {tender_id}: scanned {len(per_doc_facts)} per-doc "
            f"summaries; picked primary='{primary_doc_name or '(unknown)'}' "
            f"(score={_score_facts_richness(primary_facts)})"
        )
```

- [ ] **Step 3: Add cross-doc fallback for tender_reference**

Find the block (currently around lines 143-150):

```python
        # 1. Tender reference + portal
        ref_from_facts = _pick_first(primary_facts.get("tender_reference"))
        # Prefer GEM ref from facts (cleanest source) but verify against text
        portal, ref_from_text = _detect_portal_and_reference(synthesis_text)
        # If facts give a reference and text doesn't, derive portal from facts ref
        tender_reference = ref_from_facts or ref_from_text
        if ref_from_facts and not ref_from_text:
            facts_portal, _ = _detect_portal_and_reference(ref_from_facts)
            portal = facts_portal
```

Replace with:

```python
        # 1. Tender reference + portal — try primary doc first, then scan
        # all blobs as cross-doc fallback (the master RFP may not be the
        # first uploaded, and addenda may carry the canonical reference).
        ref_from_facts = _pick_first(primary_facts.get("tender_reference"))
        if not ref_from_facts:
            ref_from_facts = _scan_all_blobs_for(per_doc_facts, "tender_reference")
            if ref_from_facts:
                logger.info(
                    f"[enrich] tender {tender_id}: tender_reference recovered "
                    f"via cross-doc scan: '{ref_from_facts}'"
                )
        # Prefer GEM ref from facts (cleanest source) but verify against text
        portal, ref_from_text = _detect_portal_and_reference(synthesis_text)
        # If facts give a reference and text doesn't, derive portal from facts ref
        tender_reference = ref_from_facts or ref_from_text
        if ref_from_facts and not ref_from_text:
            facts_portal, _ = _detect_portal_and_reference(ref_from_facts)
            portal = facts_portal
```

- [ ] **Step 4: Add cross-doc fallback for name_of_work (scope_summary)**

Find the block (currently around lines 152-165):

```python
        # 2. Name of work (scope_summary preferred)
        name_of_work = _pick_first(
            primary_facts.get("scope_summary"),
        )
        if not name_of_work:
            # Fallback: grep the synthesis markdown for "Name of Work" row
            m = re.search(
                r"(?:Name of Work|Scope of Work)[\s\S]{0,200}?[\|:]\s*([^\|\n]{15,200})",
                synthesis_text,
                re.IGNORECASE,
            )
            if m:
                name_of_work = m.group(1).strip().rstrip("|").strip()
```

Replace with:

```python
        # 2. Name of work (scope_summary preferred) — primary, cross-doc, then markdown grep
        name_of_work = _pick_first(
            primary_facts.get("scope_summary"),
        )
        if not name_of_work:
            name_of_work = _scan_all_blobs_for(per_doc_facts, "scope_summary")
            if name_of_work:
                logger.info(
                    f"[enrich] tender {tender_id}: scope_summary recovered "
                    f"via cross-doc scan ({len(name_of_work)} chars)"
                )
        if not name_of_work:
            # Final fallback: grep the synthesis markdown for "Name of Work" row
            m = re.search(
                r"(?:Name of Work|Scope of Work)[\s\S]{0,200}?[\|:]\s*([^\|\n]{15,200})",
                synthesis_text,
                re.IGNORECASE,
            )
            if m:
                name_of_work = m.group(1).strip().rstrip("|").strip()
                logger.info(
                    f"[enrich] tender {tender_id}: name_of_work recovered "
                    f"via synthesis markdown grep"
                )
```

- [ ] **Step 5: Add cross-doc fallback for issuing_authority**

Find the block (currently around lines 166-169):

```python
        # 3. Issuing authority
        issuing_authority = _pick_first(
            primary_facts.get("issuing_authority"),
        )
```

Replace with:

```python
        # 3. Issuing authority — primary then cross-doc
        issuing_authority = _pick_first(
            primary_facts.get("issuing_authority"),
        )
        if not issuing_authority:
            issuing_authority = _scan_all_blobs_for(per_doc_facts, "issuing_authority")
```

- [ ] **Step 6: Add diagnostic log at the early-exit "nothing usable" branch**

Find:

```python
        # Nothing usable extracted → leave the row alone
        if not (tender_reference or name_of_work):
            logger.info(
                f"[enrich] tender {tender_id}: no usable reference/name extracted — skipping"
            )
            return {}
```

Replace with:

```python
        # Nothing usable extracted → leave the row alone
        if not (tender_reference or name_of_work):
            logger.info(
                f"[enrich] tender {tender_id}: no usable reference/name extracted — "
                f"skipping. Scanned {len(per_doc_facts)} per-doc summaries; "
                f"synthesis_text_len={len(synthesis_text)}"
            )
            return {}
```

- [ ] **Step 7: Add diagnostic log at the early-skip "not auto-created" branch**

Find (currently around lines 119-124):

```python
        # Only enrich auto-created (Command Center) tenders
        is_auto_created = (
            tender.portal == "command_center"
            or (tender.tender_id or "").startswith("cc-")
        )
        if not is_auto_created:
            return {}
```

Replace with:

```python
        # Only enrich auto-created (Command Center) tenders
        is_auto_created = (
            tender.portal == "command_center"
            or (tender.tender_id or "").startswith("cc-")
        )
        if not is_auto_created:
            logger.info(
                f"[enrich] tender {tender_id}: not auto-created "
                f"(portal={tender.portal!r}, tender_id={tender.tender_id!r}) — skipping"
            )
            return {}
```

- [ ] **Step 8: Add diagnostic log at the early-exit "tender not found" branch**

Find:

```python
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if not tender:
            return {}
```

Replace with:

```python
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if not tender:
            logger.info(f"[enrich] tender {tender_id}: row not found — skipping")
            return {}
```

- [ ] **Step 9: Verify Python parses**

Run: `python -c "import ast; ast.parse(open('drpl-backend/app/services/tender_enrichment_service.py', encoding='utf-8').read()); print('OK')"`
Expected: `OK`.

- [ ] **Step 10: Commit**

```bash
git add drpl-backend/app/services/tender_enrichment_service.py
git commit -m "feat(enrich): scored primary-doc picker + cross-doc fallback + diagnostic logs"
```

---

## Task 2: Confirmatory startup log for the title backfill

The backfill at startup runs every time. If it found no auto-default titles to update, it logs nothing — leaving the engineer unsure whether it ran at all.

**Files:**
- Modify: `drpl-backend/app/main.py`

- [ ] **Step 1: Always log a one-line summary, regardless of whether updates happened**

Open `drpl-backend/app/main.py`. Find this block inside `_backfill_session_titles_from_tenders` (around line 365-369):

```python
        if sessions_updated or tenders_updated:
            db.commit()
            _log.info(
                f"[DRPL] Backfilled titles: {sessions_updated} sessions, {tenders_updated} tenders"
            )
```

Replace with:

```python
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
```

This makes the backfill audit log fire on every startup, so the engineer can confirm it ran and see how many sessions it considered.

- [ ] **Step 2: Verify Python parses**

Run: `python -c "import ast; ast.parse(open('drpl-backend/app/main.py', encoding='utf-8').read()); print('OK')"`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add drpl-backend/app/main.py
git commit -m "feat(enrich): always log startup title-backfill summary"
```

---

## Verification (End of Phase 3)

After both tasks complete, do a final end-to-end pass.

- [ ] **Step 1: Parse check both files**

```bash
python -c "import ast; [ast.parse(open(p, encoding='utf-8').read()) for p in ['drpl-backend/app/services/tender_enrichment_service.py', 'drpl-backend/app/main.py']]; print('OK')"
```
Expected: `OK`.

- [ ] **Step 2: Restart the backend**

Stop uvicorn and start it again:
```
# Stop the existing uvicorn process, then:
cd drpl-backend && uvicorn app.main:app --reload --port 8000
```

On startup, look in the terminal (or `drpl-backend/logs/costing_run.log` if the analyzer + main.py loggers are routed there) for the new backfill log line, e.g.:
- `[DRPL] Title backfill ran: 0 updates needed (scanned 47 sessions across 12 tenders)` (if nothing needed updating), OR
- `[DRPL] Backfilled titles: 3 sessions, 1 tenders (scanned 47 sessions)` (if it updated rows).

If you DON'T see this log line, the function isn't being called on startup — check `app/main.py` around line 376 where it's invoked.

- [ ] **Step 3: Trigger an analyzer run and watch the enrichment logs**

In the Command Center, start a new session, upload a tender PDF, and run the Deep Analyzer. After it completes, look in `drpl-backend/logs/costing_run.log` for:

```
[enrich] tender N: scanned M per-doc summaries; picked primary='<doc-name>' (score=8)
[enrich] tender N: applied {'portal': 'IREPS', 'tender_id': '...', 'title': '...', 'session_titles_renamed': [...]}
```

The score should be > 0 for typical tenders. If you see a score of 0 (fallback to legacy first-non-empty), the per-doc key_facts didn't carry any of the recognized fields — that's a different bug worth flagging.

Cross-doc recovery logs (`recovered via cross-doc scan`) should appear when the picked primary doc was missing a field that another doc supplied. If you NEVER see them on a multi-doc tender, the primary doc is consistently rich — fine, no action needed.

- [ ] **Step 4: Open the Command Center and verify session titles**

In the Command Center dashboard, find the session you just analyzed. Its title should be `"<tender_ref> — <name_of_work>"` (e.g., `"NWR/JU/E/57/2025-26 — AMC of OHE structures, MTD substation"`).

If the title is still `"Untitled Session"` or similar default, check the log for the enrichment outcome — likely one of:
- "not auto-created" — the session was created with a non-default portal/tender_id; expected behavior.
- "no usable reference/name extracted" — per-doc summaries didn't contain identifiable fields. This is a higher-order extraction quality issue, not a Phase 3 bug.

Phase 3 ships when both these log paths fire on a normal analyzer run.

---

## Notes for the Engineer

- **Backend-only**: no frontend changes. No JS/TS compilation steps in this phase.
- **No new tests**: backend has minimal test coverage today. Manual verification via logs is the gate.
- **Per-doc `doc_name` field**: the v2 analyzer stamps `doc_name` onto each per-doc summary (see `document_analysis_agent.py:_analyze_single_document_native`). If a per-doc blob is missing `doc_name`, the diagnostic log will say `picked primary='(unknown)'` — harmless, just noting.
- **Score thresholds**: I tuned the scoring weights based on the schema in `PER_DOC_SYSTEM_PROMPT` (`tender_enrichment_service.py` references these via `_collect_per_doc_key_facts`). If real-world tender PDFs systematically score below 5 (the threshold to beat "tender_reference alone"), revisit `_score_facts_richness` weights.
- **No DB migration**: this phase doesn't touch column shapes. No Alembic revision needed.
