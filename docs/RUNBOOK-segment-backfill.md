# Runbook — Populate stored `segment` (Phase 1 backfill)

`resegment_all()` recomputes `segment` for every scored, non-overridden tender
using current thresholds. It is idempotent and respects `segment_overridden`.
The admin endpoint already exposes it.

## Safety
- Mutates the LIVE Neon prod DB. Requires explicit human go-ahead.
- Idempotent: re-running yields the same result for unchanged thresholds/scores.
- Only touches rows where `segment_overridden = False`.

## Pre-check (read-only) — how many rows will change
    SELECT count(*) FROM tenders
    WHERE ai_relevance_score IS NOT NULL AND segment_overridden = false;

## Execute (choose ONE)
### Option A — admin endpoint (preferred; goes through app auth)
    PUT /api/admin/tender-scoring/settings?resegment=true
    Body: {}   (empty JSON object is fine — no settings need changing)
    Auth: master_admin JWT. This is the settings-update route; passing
    resegment=true runs resegment_all() as a side-effect after applying the
    (here empty) settings body. See tender_scoring_admin.py:put_settings_route.

### Option B — one-off script (only if the endpoint is unavailable)
    cd drpl-backend
    venv/Scripts/python.exe -c "from app.core.database import SessionLocal; \
    from app.services.auto_scoring_service import resegment_all; \
    db=SessionLocal(); print(resegment_all(db)); db.close()"

## Verify
    SELECT segment, count(*) FROM tenders
    WHERE ai_relevance_score IS NOT NULL GROUP BY segment;
(expect no NULL segment among scored, non-overridden rows)

## Rollback
No destructive change — segment is derived. Re-running with prior thresholds
restores prior values; manual overrides are never touched.
