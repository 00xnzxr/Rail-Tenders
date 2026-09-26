# SOP — Agent Runs + RQ Pipeline

## Goal

Long-running agent invocations are dispatched to RQ so the web process stays responsive. Clients subscribe to live output via SSE. Capacity is observable for backpressure and load testing.

## Inputs

- **Enqueue endpoint.** `POST /api/runs/enqueue` at [../app/api/routes/runs.py:138-301](../app/api/routes/runs.py).
  - Auth: `current_user` (JWT).
  - Pre-checks: global queue depth ([runs.py:160](../app/api/routes/runs.py) `GLOBAL_QUEUE_MAX`), per-user active cap ([runs.py:166-167](../app/api/routes/runs.py) via `redis_client.scard(_user_active_key(user_id))`).
  - Behavior on capacity breach: 429 with retry-after, OR soft-fail through ([runs.py:173-178](../app/api/routes/runs.py)) — see Failure modes.
  - Wraps `run_id_scope(run.id)` at [runs.py:290-294](../app/api/routes/runs.py).
- **SSE consumer.** `GET /api/runs/{run_id}/events` at [runs.py:338-415](../app/api/routes/runs.py).
  - Reads from Redis Stream `run_stream_key(run_id)`.
  - XREAD blocking loop with `block=5000, count=100` ([runs.py:374](../app/api/routes/runs.py)).
- **RQ job entrypoint.** `run_router_task(run_id)` at [../app/worker/run_tasks.py:45-50](../app/worker/run_tasks.py).
  - Gold-standard `run_id_scope` wrap: `with run_id_scope(run_id): return _run_router_task_inner(run_id)`. Every SOP that adds a new background entry should copy this pattern verbatim.

## Outputs

- **DB.** `AgentRun` row with terminal status (`succeeded` / `failed` / `cancelled`) + `result` JSON.
- **Redis Stream.** Token deltas, tool calls, tool results, final status event consumed by SSE clients.
- **Capacity snapshot.** `GET /health/capacity` at [../app/main.py:560-638](../app/main.py) — used by [../scripts/load_test_command_center.py](../scripts/load_test_command_center.py) and dashboards.

## Determinism boundary

This is orchestration — no LLM business logic. The RQ layer is fully deterministic. The graphs invoked downstream (`costing_researcher`, `tender_doc_analyzer`, etc.) carry their own SOPs.

## Tool contract

No `BaseTool` invocations at the RQ layer. The tools are inside the graphs the RQ job dispatches to. See [sop-costing-pipeline.md](sop-costing-pipeline.md) and [sop-tender-analyzer-v2.md](sop-tender-analyzer-v2.md).

## Edge cases

0. **Cancelled by the user.** `POST /runs/{run_id}/cancel`, ownership-checked, idempotent, 503 when Redis is unreachable — because Redis is how the web process reaches the worker, and answering 200 would claim a stop that did not happen. See "Open work" below for the mechanism and its one limitation.

1. **Killed mid-flight.** SIGKILL / deploy interruptions leave rows in `running` status with no terminal write. Two reapers run at startup, one per table: `_reap_stuck_executions_on_startup()` for `AgentExecution`, and `_reap_stuck_runs_on_startup()` → `run_service.reap_stuck_runs()` for `AgentRun`. The second did not exist until 2026-09-07: an `AgentRun` stranded at `running` stayed that way forever, and a browser reattaching to it waited on a Redis stream that would never emit `run_done` — a spinner that turned until the SSE endpoint's own 40-minute deadline. The events endpoint now also probes the row on each keepalive tick and closes the stream when it has gone terminal without a sentinel, so the two mechanisms cover each other.
2. **Per-user concurrency cap.** Stored in a Redis SET; `enqueue` checks size, RQ task removes on terminal status. Race: if a run terminates without releasing the slot, the user is locked out. Mitigation: the slot is released best-effort at [run_tasks.py:129-134](../app/worker/run_tasks.py).
3. **SSE reconnect.** `last_id` defaults to `"0"`, which replays the run's whole history — that is what the frontend uses, and it is correct: the client rebuilds the answer from the beginning rather than trying to splice onto a partial one. The `last_id` cursor remains available for a caller that wants to resume mid-stream, but note that verbatim (worker-published) entries carry no `id:` line, so a client cannot currently track one.

4. **Reattaching without a local pointer.** `GET /runs/active?proposal_session_id=` returns the newest run for that session still `queued`/`running` and not stranded. The frontend's `localStorage` pointer is only a shortcut; it lives in the browser that started the run, so a second device, another browser, or cleared site data had no way back to work that was still very much alive. Stranded rows are excluded here as well as reaped, so this endpoint can never hand out a stream that will produce no further events.

**Staleness is one predicate, `run_service._stale_run_clause`, shared by the reaper and this endpoint** so the two can never disagree about whether a run is alive. It measures each status from the clock that status actually runs on:

| Status | Measured from | Cutoff |
|---|---|---|
| `running` | `coalesce(started_at, created_at)` | `_RUN_JOB_TIMEOUT_SECONDS` + `_REAP_GRACE_SECONDS` (30m + 5m) |
| `queued` | `created_at` | `_REAP_QUEUED_AFTER_SECONDS` (2× the job timeout, 60m) |

Both cutoffs were originally `created_at + job_timeout`, which is wrong in one specific and common way: RQ's `job_timeout` starts when a **worker picks the job up**, not when it was enqueued. A run that waited ten minutes behind the queue — twelve worker slots, and `/health/capacity` exists because they fill — and then ran for twenty-five was 35 minutes old while sitting inside its 30-minute timeout. The next web boot cancelled it out from under the worker still executing it, and this endpoint reported "no active run" to a user reattaching from another device. The Master's own wall-clock budget is the full 1800s, so any queue wait at all was enough to trigger it.

A `queued` row the reaper closed is not run when RQ finally hands it to a worker: `run_tasks` checks both the cancel flag and the row's own status before marking a job running, and a row that says `cancelled` is not started, whoever said it — the user was already told. The three timeouts stay ordered — 30m job timeout < 35m reap < 40m SSE hard deadline — so the worker gets its full budget, the row goes terminal after it, and the stream's backstop fires last.
4. **Capacity health check returns 200 even under pressure.** `/health/capacity` reports numbers; it does not gate requests. Liveness vs. backpressure are separate concerns.

## Failure modes

| Anchor | What happens | Classification |
|---|---|---|
| [run_tasks.py:98-101](../app/worker/run_tasks.py) | Exception during `_pump()` caught → run marked `failed` | **Intentional.** Status row is the canonical truth — clients see `failed`, not an exception. |
| [run_tasks.py:111-114](../app/worker/run_tasks.py) | Status-write exception suppressed (`db.rollback`) | **Intentional best-effort.** If the status write fails, the reaper will eventually catch it. |
| [run_tasks.py:129-134](../app/worker/run_tasks.py) | Active-slot release exception swallowed | **Intentional best-effort.** Soft race: the user may be temporarily over their cap. |
| [runs.py:173-178](../app/api/routes/runs.py) | Capacity pre-check exception → soft-fail allow | **Intentional.** Better to enqueue than to 500 on a stale Redis read. The actual RQ queue still has its own size limit. |

**The contract.** When in doubt, the `AgentRun` row status is canonical. SSE events are convenience; the DB row is truth.

## Verification

```bash
cd drpl-backend

# 1. Enqueue a run via curl (replace TOKEN + payload)
curl -X POST localhost:8000/api/runs/enqueue \
  -H "Authorization: Bearer $TOKEN" \
  -H "content-type: application/json" \
  -d '{"agent_key": "costing_researcher", "session_id": 1, "message": "..."}'

# 2. Subscribe to the stream (use the returned run_id)
curl -N localhost:8000/api/runs/$RUN_ID/events

# 3. Snapshot capacity at peak
curl localhost:8000/health/capacity | jq

# 4. Full load test
python scripts/load_test_command_center.py --runs 20 --concurrency 5
# Exit 0 = pass; reports p50/p95 + TTFT budget compliance

# 5. Verify run_id stamps in worker logs
grep -c "\[run=" logs/*.log
```

## Open work

- **Partial output is on the row and, for a run that did not finish, in the session history.** `_pump` rebuilds what the run has said and done from the events it fans out (`token` appends, `token_reset` discards a model turn's pre-tool narration, `decision_action`/`decision_observation` become a step list) and writes it to `agent_runs.partial_output` / `partial_trace` every five seconds and on exit, on its own short session. A run ending `cancelled` or `failed` has that written into the session transcript by the worker (`run_service.save_partial_turn`), and a worker killed outright — which writes nothing at the end — has it written by the startup reaper when it closes the row. Idempotent: nothing is written when an assistant turn already exists since the run started, so a run whose answer landed but whose status write failed does not get a "partial" copy of its own answer. Redis still replays the full stream for an hour; this is what outlives it. `result_summary` and `selected_agents` remain unwritten.
- **Cancellation is cooperative, and its latency is one event boundary.** `POST /runs/{run_id}/cancel` sets `drpl:run:{id}:cancel`; `_pump` reads it between the SSE chunks it fans out and closes the generator when it is set. A run that is *queued* is cancelled outright — the RQ job is cancelled, the row closed, the stream ended and the user's concurrency slot released, and no model call is ever spent on it. A run that is *running* is asked, and reports `cancelling` rather than `cancelled` until the worker writes the terminal status itself. The worker re-checks both the flag and the row's status before it marks a job running, which is what actually guarantees a cancelled run never starts: RQ's own cancel cannot pull back a job already handed to a worker, and the flag is deliberately left in place (it has a TTL) so a worker that dequeued a moment before the cancel still sees it. The probe inside `_pump` is throttled to once a second — one Redis round trip per streamed token was a cost nobody asked for. Not a kill — a work horse killed mid-statement leaves this pipeline's artifact, message and usage writes half-done with no `finally`. The gap that remains is granularity: a worker sitting inside one long model call emits nothing, so the stop lands when that call returns.
- **Attachments are resolved in the enqueue POST**, not in the worker — the same ~20s pre-stream window that was removed from the inline path still exists here, and a client that leaves during it never receives a run id.

The two adjacent SOPs ([sop-tender-analyzer-v2.md](sop-tender-analyzer-v2.md), [sop-costing-pipeline.md](sop-costing-pipeline.md)) are catching up to the `run_id_scope` discipline this surface already has.
