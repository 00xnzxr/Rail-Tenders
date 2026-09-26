# Backend Architecture SOPs

Standard Operating Procedures for the backend agentic surfaces. Each SOP follows the same shape:

- **Goal** — what this system is for
- **Inputs** — exact entry points and payload shapes
- **Outputs** — exact result shapes and side effects
- **Determinism boundary** — what the LLM is allowed to decide vs. what must be computed by a tool
- **Tool contract** — which tools are invoked and what they return
- **Edge cases** — known sharp edges, with anchors
- **Failure modes** — what breaks, what is swallowed intentionally, what is a defect
- **Verification** — a single command (or short sequence) that proves the system still works

## SOPs

| SOP | Surface | Status |
|---|---|---|
| [sop-tender-analyzer-v2.md](sop-tender-analyzer-v2.md) | Vision-first PDF extraction + synthesis | Active |
| [sop-costing-pipeline.md](sop-costing-pipeline.md) | Costing graphs + tools, with determinism fix | Active |
| [sop-agent-runs-rq.md](sop-agent-runs-rq.md) | `/api/runs/enqueue` → RQ → SSE pipeline | Active |

## Cross-cutting infrastructure

Not a surface — referenced by every SOP:

- **LLM dispatch.** `app/services/langchain/llm_factory.get_chat_model()` with failover via `FailoverChatModel`. Never instantiate provider clients directly.
- **Storage.** `app/services/storage_service` switches local ↔ R2 by `settings.storage_backend`. Never call boto3 directly.
- **Run-ID logging.** `app/core/run_context.run_id_scope(run_id)` — wrap every background entrypoint.
- **Schema drift fixes.** `_apply_schema_drift_fixes()` and `_add_missing_columns()` in `app/main.py` self-heal column drift on Railway deploys.
