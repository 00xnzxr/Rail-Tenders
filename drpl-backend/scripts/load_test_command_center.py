"""
DRPL Command Center — Load Test

Submits N agent runs in parallel against a running deployment, streams each
run's SSE output to completion, and reports per-run timing + aggregate
p50/p95 + a snapshot of /health/capacity at the peak of the test.

Usage (Railway, already deployed):
    python -m scripts.load_test_command_center \
        --base-url https://drpl-backend-production.up.railway.app \
        --token "$DRPL_JWT" \
        --session-id 42 --concurrency 10

Prereqs:
    pip install httpx

Exit code is non-zero if any run ends in `failed` / `cancelled` or if p95
time-to-first-token exceeds --ttft-budget-ms.

Hits the RQ-backed endpoints:
    POST /api/runs/enqueue      → 200 { run_id, rq_job_id, events_url, ... }
    GET  /api/runs/{id}/events  → text/event-stream (Redis Streams replay)

If the deployment's Redis plugin isn't wired up, /api/runs/enqueue returns
503 and the test aborts — that's the intended failure mode.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import httpx
except ImportError:
    print("ERROR: pip install httpx", file=sys.stderr)
    sys.exit(2)


DEFAULT_MESSAGE = (
    "Give me a one-paragraph summary of tender analysis best practices. "
    "Keep it short; this is a load test."
)


@dataclass
class RunResult:
    run_id: Optional[str] = None
    submit_latency_ms: Optional[float] = None
    ttft_ms: Optional[float] = None          # submit -> first 'sse' event
    total_ms: Optional[float] = None         # submit -> run_done
    terminal_status: Optional[str] = None    # completed | failed | timeout
    events_received: int = 0
    error: Optional[str] = None
    first_token_seen: bool = False
    _submit_start: float = field(default=0.0, repr=False)


async def _one_run(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    session_id: Optional[int],
    message: str,
    timeout_s: float,
) -> RunResult:
    r = RunResult()
    headers = {"Authorization": f"Bearer {token}"}

    # --- Submit ---
    body = {"message": message}
    if session_id is not None:
        body["proposal_session_id"] = session_id
    r._submit_start = time.perf_counter()
    try:
        resp = await client.post(
            f"{base_url}/api/runs/enqueue",
            headers=headers,
            json=body,
            timeout=30.0,
        )
    except Exception as e:
        r.error = f"submit exception: {e}"
        r.terminal_status = "failed"
        return r
    r.submit_latency_ms = (time.perf_counter() - r._submit_start) * 1000
    if resp.status_code != 200:
        r.error = f"submit HTTP {resp.status_code}: {resp.text[:200]}"
        r.terminal_status = "failed"
        return r
    data = resp.json()
    r.run_id = data["run_id"]

    # --- Stream ---
    stream_url = f"{base_url}/api/runs/{r.run_id}/events"
    deadline = r._submit_start + timeout_s
    current_event = ""
    try:
        async with client.stream(
            "GET", stream_url, headers=headers, timeout=timeout_s,
        ) as stream:
            if stream.status_code != 200:
                r.error = f"stream HTTP {stream.status_code}"
                r.terminal_status = "failed"
                return r
            async for raw_line in stream.aiter_lines():
                if time.perf_counter() > deadline:
                    r.error = "stream deadline exceeded"
                    r.terminal_status = "timeout"
                    return r
                if not raw_line:
                    current_event = ""
                    continue
                if raw_line.startswith(":"):
                    continue  # keepalive comment
                if raw_line.startswith("event: "):
                    current_event = raw_line[7:].strip()
                elif raw_line.startswith("data: "):
                    r.events_received += 1
                    # On the remote, most chunks come through as event "sse"
                    # (verbatim command-center SSE replayed). Any data line
                    # counts as forward progress for TTFT.
                    if not r.first_token_seen:
                        r.first_token_seen = True
                        r.ttft_ms = (time.perf_counter() - r._submit_start) * 1000
                    if current_event == "run_done":
                        try:
                            payload = json.loads(raw_line[6:])
                            r.terminal_status = payload.get("status", "completed")
                        except Exception:
                            r.terminal_status = "completed"
                        r.total_ms = (time.perf_counter() - r._submit_start) * 1000
                        return r
                    if current_event == "error":
                        try:
                            payload = json.loads(raw_line[6:])
                            r.error = payload.get("message", "")
                        except Exception:
                            pass
    except Exception as e:
        r.error = f"stream exception: {e}"
        r.terminal_status = "failed"
        return r

    r.terminal_status = r.terminal_status or "timeout"
    return r


async def _capacity_watcher(
    client: httpx.AsyncClient,
    base_url: str,
    stop: asyncio.Event,
    samples: list,
):
    while not stop.is_set():
        try:
            resp = await client.get(f"{base_url}/health/capacity", timeout=5.0)
            if resp.status_code == 200:
                samples.append({"t": time.time(), **resp.json()})
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            continue


def _percentile(vals: list[float], p: float) -> Optional[float]:
    if not vals:
        return None
    vals = sorted(vals)
    idx = int(round((p / 100.0) * (len(vals) - 1)))
    return vals[idx]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="e.g. https://drpl-backend-production.up.railway.app")
    ap.add_argument("--token", required=True, help="JWT for the test user")
    ap.add_argument("--session-id", type=int, help="Existing proposal_session_id to run against (optional)")
    ap.add_argument("--concurrency", type=int, default=10, help="Parallel runs to launch")
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--per-run-timeout", type=float, default=300.0, help="Seconds")
    ap.add_argument("--ttft-budget-ms", type=float, default=5000.0,
                    help="Fail the test if p95 time-to-first-token exceeds this")
    args = ap.parse_args()

    base_url = args.base_url.rstrip("/")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # --- Baseline capacity ---
        try:
            resp = await client.get(f"{base_url}/health/capacity", timeout=5.0)
            print(f"baseline capacity: {resp.json()}")
        except Exception as e:
            print(f"baseline capacity check failed: {e}")

        # --- Fire N runs in parallel ---
        stop = asyncio.Event()
        samples: list = []
        watcher = asyncio.create_task(
            _capacity_watcher(client, base_url, stop, samples)
        )
        print(f"launching {args.concurrency} concurrent runs (session_id={args.session_id})...")
        t0 = time.perf_counter()
        results: list[RunResult] = await asyncio.gather(
            *[
                _one_run(
                    client, base_url, args.token, args.session_id,
                    args.message, args.per_run_timeout,
                )
                for _ in range(args.concurrency)
            ],
            return_exceptions=False,
        )
        wall_s = time.perf_counter() - t0
        stop.set()
        await watcher

    # --- Report ---
    print(f"\nwall time: {wall_s:.2f}s")
    ok = sum(1 for r in results if r.terminal_status == "completed")
    fail = sum(1 for r in results if r.terminal_status == "failed")
    timeout = sum(1 for r in results if r.terminal_status == "timeout")
    print(f"terminal: {ok} ok, {fail} failed, {timeout} timeout")

    submits = [r.submit_latency_ms for r in results if r.submit_latency_ms is not None]
    ttfts = [r.ttft_ms for r in results if r.ttft_ms is not None]
    totals = [r.total_ms for r in results if r.total_ms is not None]

    def _fmt(vals):
        if not vals:
            return "n/a"
        return (
            f"p50={_percentile(vals, 50):.0f}ms "
            f"p95={_percentile(vals, 95):.0f}ms "
            f"max={max(vals):.0f}ms "
            f"(n={len(vals)})"
        )

    print(f"submit:      {_fmt(submits)}")
    print(f"TTFT:        {_fmt(ttfts)}")
    print(f"total:       {_fmt(totals)}")

    if samples:
        v2s = [s.get("v2", {}) for s in samples]
        queue_depths = [v.get("queue_depth", 0) or 0 for v in v2s]
        active_global = [v.get("active_runs_global", 0) or 0 for v in v2s]
        if queue_depths:
            print(f"queue_depth: peak={max(queue_depths)} avg={statistics.mean(queue_depths):.1f}")
        if active_global:
            print(f"active_runs: peak={max(active_global)} avg={statistics.mean(active_global):.1f}")
        l429 = [v.get("llm_429_last_5m", 0) or 0 for v in v2s]
        if l429:
            print(f"llm_429_peak: {max(l429)}")

    # --- Error detail ---
    for r in results:
        if r.terminal_status != "completed" and r.error:
            print(f"  run={r.run_id} status={r.terminal_status} error={r.error!r}")

    # --- Exit code ---
    p95_ttft = _percentile(ttfts, 95) if ttfts else None
    failed_any = fail + timeout
    if failed_any:
        print(f"\nFAIL: {failed_any} run(s) did not complete cleanly")
        sys.exit(1)
    if p95_ttft is not None and p95_ttft > args.ttft_budget_ms:
        print(f"\nFAIL: p95 TTFT {p95_ttft:.0f}ms > budget {args.ttft_budget_ms:.0f}ms")
        sys.exit(1)
    print("\nPASS")


if __name__ == "__main__":
    asyncio.run(main())
