# Backend — Task Plan

## North Star

Apply the System Pilot protocol to the backend agentic surfaces: tender analyzer v2, costing pipeline, agent runs + RQ.

## Phases

| Phase | State | Notes |
|---|---|---|
| Blueprint | ✅ done | Captured in [../CONSTITUTION.md](../CONSTITUTION.md) — Data Schemas, Behavioral Rules, B.L.A.S.T. outputs |
| Link | 🟡 in progress | Probes in [../execution/probes/](../execution/probes/) — anthropic, openai, google, postgres, redis, r2 |
| Architect | ✅ done | SOPs in [../architecture/](../architecture/) — three surface SOPs + README |
| Stylize | n/a | Output formatters live in existing services; no new layer added |
| Trigger | n/a | Existing Railway web + worker deployments unchanged |

## Active surfaces

- **Tender analyzer v2** → [../architecture/sop-tender-analyzer-v2.md](../architecture/sop-tender-analyzer-v2.md)
- **Costing pipeline** → [../architecture/sop-costing-pipeline.md](../architecture/sop-costing-pipeline.md)
- **Agent runs + RQ** → [../architecture/sop-agent-runs-rq.md](../architecture/sop-agent-runs-rq.md)

## Open code changes from the System Pilot pass

1. **Costing pipeline — determinism fix.** Promote `cost_calculator` invocation from optional to mandatory in `costing_agent.py` system prompt. LLM transcribes `amount` from tool output instead of computing.
2. **`run_id_scope` wrap on `run_document_analysis`** at `app/services/langchain/graphs/document_analysis_agent.py:241`.
3. **`run_id_scope` wrap on costing graph entrypoints** (`run_costing_research`, `run_enhanced_costing_research`) — only if not already wrapped by upstream callers.

Each change is logged in [progress.md](progress.md) as it lands and the rationale captured in [decisions.md](decisions.md).
