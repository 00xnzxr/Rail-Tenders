# Backend — Progress Log

Append one entry per probe run or code change. Newest at the top.

| Date | Actor | Event | Detail |
|---|---|---|---|
| 2026-05-18 | claude | scaffold | Created `memory/`, `architecture/`, `execution/probes/`, `.tmp/` |
| 2026-05-18 | claude | constitution | Wrote `../CONSTITUTION.md` |
| 2026-05-18 | claude | sop | Wrote `../architecture/{README,sop-tender-analyzer-v2,sop-costing-pipeline,sop-agent-runs-rq}.md` |
| 2026-05-18 | claude | memory | Seeded `task_plan.md`, `findings.md`, `decisions.md` |
| 2026-05-18 | claude | probe | Wrote 7 probe scripts: `_common.py`, `anthropic.py`, `openai.py`, `google.py`, `postgres.py`, `redis.py`, `r2.py` |
| 2026-05-18 | claude | code | Added `current_run_id()` helper to `app/core/run_context.py` |
| 2026-05-18 | claude | code | Wrapped `run_document_analysis` in `run_id_scope` (conditional — inherits caller's run_id if present) |
| 2026-05-18 | claude | code | Wrapped `run_enhanced_costing_research` in `run_id_scope` (conditional) |
| 2026-05-18 | claude | code | Costing prompt: added RULE 4 (mandatory `cost_calculator` invocation + `amount` transcription); updated TOTALS section, MANDATORY REASONING step 6, MINIMUM VIABLE OUTPUT failure modes |
| 2026-05-18 | verify | compile | `python -m py_compile` green on all 4 modified files + all 7 probes |
| 2026-05-18 | verify | pytest | SKIPPED — no project venv in workspace; tests run on Railway/CI |
