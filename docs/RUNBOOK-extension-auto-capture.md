# Runbook — Extension gated auto-capture (Piece B) smoke test

Auto-capture is gated on the backend flag `extension_auto_capture_enabled`
(surfaced as `features.auto_capture`, default OFF). The orchestration
(background tabs / fetch) is not unit-testable; verify by hand.

## Enable
1. Backend: set `EXTENSION_AUTO_CAPTURE_ENABLED=true` (env) or flip the config
   default, and redeploy / restart so `/api/extension/config` returns
   `features.auto_capture: true`.
2. Extension: load `drpl-extension/dist/` unpacked (chrome://extensions), log in,
   and let it sync `/config` (reopen the popup once to force a fetch).

## Verify
1. Navigate to a portal listing (IREPS/GeM/aggregator) that contains a tender
   you know is in scope (matches a keyword_group keyword, a target ministry,
   and value within [value_min, value_max]) AND one clearly out of scope.
2. Run a scrape from the popup.
3. Expect: after metadata upload, the in-scope tender's detail page opens in a
   background tab automatically and its PDFs upload (watch the service-worker
   console: `[Ext] Auto-capture: N in-scope`). The out-of-scope tender's docs
   are NOT auto-captured and it remains under "Hunt Documents".
4. Cap check: if >10 in-scope tenders match, confirm exactly 10 are auto-captured
   and the notification/console reports the overflow left for manual hunt, and
   the manual "Hunt Documents" button still lists the remainder.
5. Off check: set the flag back to false, re-sync, scrape again — behavior is
   identical to today (manual button only, nothing auto-runs).
