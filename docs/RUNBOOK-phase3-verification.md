# Runbook — Phase 3 verification gates

Both gates below are infra-bound and must be executed by a human on the deploy
host — they need a running RQ worker + Redis and/or the prod NIT file in R2,
which the dev host cannot reach.

## 3a. Eager-analysis live smoke (needs RQ worker + Redis)
1. Set `REDIS_URL`; start the worker: `cd drpl-backend && venv/Scripts/python.exe worker.py`
2. Set `EAGER_ANALYSIS_ENABLED=true` in the worker env; restart the worker.
3. Ensure an in-scope tender (score >= 0.70 or eligible-flagged) with >=1 uploaded
   doc exists.
4. Observe: the per-tender status marker goes `queued` -> `running` -> `done`; a
   `DocumentExtractionResult` row is created. Confirm daily-cap + backpressure
   behave (watch `logs/costing_run.log` and the worker log).
5. Revert `EAGER_ANALYSIS_ENABLED` to false when done, unless enabling for real.

## 3b. NIT full UI costing run (needs prod NIT file in R2)
1. From the UI, run a full costing on a reference tender (e.g. #2531).
2. Download the generated XLSX; eyeball the reconciliation block and confirm the
   locked-NIT number-world matches the known-good total (60,879,392.16 for #2531).
3. Note any fabrication / `needs_review` flags.

Both are infra-bound and must be executed by a human on the deploy host.
