# Platform Tools + Output-Quality Diagnosis

Date: 2026-09-02
Status: implemented

Sub-projects **B** and **C** of the autonomous-platform decomposition.
A + D shipped first — see
[the global assistant design](2026-09-02-global-assistant-and-confirm-gate-design.md).

## B — Broad tool + parameter access

### The problem the gate does not solve

The write-confirmation gate asks the *user* whether to proceed. It does not ask
whether they are **allowed** to. Those are different questions, and conflating
them is privilege escalation wearing a safety feature's clothes: an operator
asks the assistant to change a master-admin-only setting, sees a friendly
confirmation card, and clicks "Yes".

So capability here is bounded by role, server-side, and the role is resolved
**from the database inside `run_decision_maker`** — never from anything the
client sent. A role in a request body would be a trivial escalation.

### Design

`app/services/langchain/graphs/platform_tools.py`. Each tool declares a minimum
role, enforced twice:

- **Build time** — tools above the caller's role are omitted, so the agent does
  not plan around capabilities it will never be granted. This is a UX
  affordance, not a control.
- **Call time** — the real control. An agent hallucinating a tool name, or a
  future refactor that forgets to pass the role, must still be refused.

Refusals are returned as observation strings, matching how the gate suspends:
the agent reads the refusal and explains it, rather than crashing the run.

| Tool | Min role | Tier |
|---|---|---|
| `list_platform_settings` | admin | read |
| `get_platform_health` | admin | read |
| `list_recent_agent_runs` | admin | read |
| `list_platform_users` | master_admin | read |
| `update_platform_setting` | master_admin | **write** (gated) |

### Secrets

`PlatformSetting.is_secret` values are redacted on read and rejected on write.
An agent that can print an API key into a chat transcript has exfiltrated it,
regardless of how the conversation started.

### Writing settings

- An **unknown key is refused, never created.** An agent inventing a key would
  write a setting nothing reads, and the user would believe it took effect.
- **The stored type is preserved.** A bool silently becoming the string
  `"maybe"` reads as truthy everywhere downstream — the worst kind of config bug.

## C — Output-quality diagnosis

`app/services/langchain/graphs/quality_tools.py`, one read-only tool:
`diagnose_tender_outputs`.

Detection is **deterministic Python, not an LLM judging its own work** —
Constitution Rule 1, and also the only way the answer is trustworthy. The
agent's job is to explain the finding and offer to fix it, not to decide what
counts as broken.

The checks are this codebase's own documented failure modes, not generic
"does this look right" prompting:

| Check | Why it exists |
|---|---|
| Empty/near-empty analysis | `config.py` records multi-PDF tenders producing "empty synthesis output even though each individual call succeeded" — the reason `tender_analyzer_max_parallel` is pinned to 1. |
| Paraphrased annexures | The two-pass annexure design exists because one pass "silently compressed — … substituting descriptive '[Name of Bidder]' placeholders where the source had dotted rules". Those strings are a fingerprint: a verbatim transcription carries the blank, not a description of it. |
| Empty documents | A document row with no content is a generation that silently produced nothing. |
| Failed/stuck runs | `failed` and `timeout` executions. |

The placeholder check is deliberately narrow. Real tender text uses brackets
constantly (`[see Annexure II]`, clause references), so a broad bracket match
would make the tool noise and train users to ignore it.

Document names are resolved from the linked `ChecklistItem` —
`DocumentWorkspace` has no name column, and a report saying "document #417" is
useless to the reader.

## Testing

    cd drpl-backend && .venv/bin/pytest tests/ -q    # 581 passed, 1 skipped
    cd drpl-frontend && npm test                      # 22 passed
    cd drpl-frontend && npm run build                 # tsc -b clean

- `test_platform_tools.py` — the role ladder (including default-deny for
  unknown/missing/wrong-case roles), build-time filtering, call-time refusal
  when a tool is reached anyway, secret redaction and write rejection, unknown
  keys, type preservation, and that the user listing carries no password
  material.
- `test_quality_tools.py` — the placeholder fingerprint and, importantly, that
  legitimate bracketed tender text is *not* flagged; each problem shape; that
  diagnosis never mutates.
- `test_confirm_gate_e2e.py` — role bounding driven through the real
  LangGraph loop with a scripted model.
- `test_tool_policy.py`'s drift guard now classifies the platform and quality
  catalogs too, so a tool added later cannot slip through unclassified.
