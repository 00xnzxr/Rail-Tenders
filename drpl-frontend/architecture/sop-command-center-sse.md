# SOP — Command Center SSE Consumer

## Goal

Render live agent run output as it streams from the backend. The user submits a message, the backend enqueues a run, and the frontend subscribes to the Redis-Stream-backed SSE endpoint until the run reaches a terminal status.

## Inputs

- **Backend producer.** `GET /api/runs/{run_id}/events` at [../../drpl-backend/app/api/routes/runs.py:338-415](../../drpl-backend/app/api/routes/runs.py).
- **Frontend consumer.** Components under `src/components/command-center/`.
- **Auth.** Bearer token from `localStorage.drpl_token`, attached via `EventSource` polyfill or `fetch` + `ReadableStream`.

## Outputs

Streaming UI: token-by-token output, tool-call cards, tool-result cards, terminal status banner.

## Event shape (contract)

```
event: token         → { delta: string, ts: number }
event: tool_call     → { name: string, args: object, id: string }
event: tool_result   → { id: string, output: string }
event: status        → { status: "succeeded" | "failed" | "cancelled", result?: object }
```

**Forward-compatibility rule.** Unknown event names are logged + skipped, NOT thrown. The backend may add new event types without a synchronized frontend release.

## Determinism boundary

The frontend renders bytes. It does not retry, summarize, or interpret. If a `status: failed` event arrives, show the error message — do not optimistically retry the run.

## Edge cases

1. **Reconnect.** On network drop, the EventSource native reconnect is fine for short outages. For >30s gaps, use the SSE `id:` field as a cursor and resume via `?after=<cursor>`. The backend supports this — see [runs.py:374](../../drpl-backend/app/api/routes/runs.py) XREAD loop.
2. **401 mid-stream.** Token expired. Close the stream; redirect to `/login` (consistent with `src/lib/api.ts`'s 401 handler).
3. **Run already terminal when subscribing.** The stream replays buffered events from the start, then emits the terminal `status` event. Frontend should not assume the first event is `token`.
4. **Multiple subscribers per run.** Allowed — Redis Streams support fan-out. Each subscriber gets the full sequence.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Stream stalls with no events | Backend RQ worker died mid-run; reaper hasn't fired yet | Wait for next backend restart (reaper at [main.py:379-390](../../drpl-backend/app/main.py)) flips status; OR poll `/api/runs/{id}` directly |
| Token deltas arrive out of order | EventSource polyfill bug | Use native EventSource; or accept that order within a single SSE connection is guaranteed by the protocol |
| Tool result missing for a tool call | Tool errored AND its error event was swallowed at the graph layer | Check backend logs at the `[run=<id>]` stamp |

## Verification

```bash
# 1. Backend health
node execution/probes/backend-health.mjs

# 2. End-to-end stream test (using curl)
TOKEN=$(cat ~/.drpl_token)
RUN=$(curl -s -X POST localhost:8000/api/runs/enqueue \
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"agent_key":"costing_researcher","session_id":1,"message":"test"}' | jq -r .run_id)
curl -N -H "Authorization: Bearer $TOKEN" localhost:8000/api/runs/$RUN/events
# Should stream events ending with `event: status\ndata: {"status":"succeeded",...}`

# 3. Visual: open the Command Center in the browser, submit a message,
#    confirm tokens stream + tool cards render
```

## Open work

None for the basic contract. Resume-from-cursor on reconnect is implemented backend-side but is not exercised by all frontend consumers — verify per-component.
