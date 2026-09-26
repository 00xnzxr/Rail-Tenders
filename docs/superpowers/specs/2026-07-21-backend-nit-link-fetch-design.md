# Backend NIT Link Fetch (Public-Link Fallback) — Design

**Date:** 2026-07-21
**Status:** Approved (brainstorming) — pending implementation plan
**Related:** [[nit-fetch-and-enrichment]] (`2026-07-21-nit-fetch-and-enrichment-design.md`) — this builds on it.

## Problem / insight

The extension downloads a tender's NIT via its authenticated portal session, but it only runs when the user scrapes. Many tenders never get their NIT captured, so the analyzer/scoring agent has no NIT to read. The user observed that **the IREPS NIT link is public — viewable without login** — so the backend can fetch it directly, as a fallback that doesn't depend on the extension.

## Verified foundation

- IREPS `nit_document_links` are static public PDFs, e.g. `https://www.ireps.gov.in/ireps/works/pdfdocs/082026/89872774/viewNitPdf_5425319.pdf`. A plain backend GET returns `200`, `content-type: application/pdf`, `%PDF-1.4`, ~333KB — **no login, no HTML redirect**. (Tested live 2026-07-21.)
- Coverage: **2224 / 2229 IREPS tenders** have a NIT link. GeM (87 tenders) has **none** (different mechanism — stays on the extension path). Aggregators may have public links too.

## Decisions (from brainstorming)

- **Portal-generic:** "if there's a public document URL, the backend fetches it." Publicness is verified per-URL at fetch time (content-type must be a document, not an HTML login page), so the mechanism self-selects — public links download, gated ones are rejected and fall back to the extension.
- **Both triggers:** eager background fetch for newly-scraped tenders + on-demand fallback at analysis/scoring time for older tenders.
- **Fetch feeds the existing pipeline** — no new agent logic. The "loop" is: missing NIT → fetch from link → analyzer + the merged enrichment (title/scope/bid value/EMD/date) run on it. The scoring/analysis agent simply gets better input.
- **Guard: host allowlist + content checks** (tightest). Backend only ever reaches real tender portals.
- **Dedup:** reuse the extension upload's `(tender, source_url)` dedup so the two paths cooperate (whichever fetches first wins; the other no-ops).

## Non-goals

- No GeM backend fetch (no public link; GeM stays extension-only).
- No new scoring/analysis agent logic; no retry/escalation loop (fetch-and-feed only).
- No schema change (uses existing `TenderDocument.source_url`).

## Architecture

### 1. `nit_link_fetch_service` (the one place that fetches external URLs)

`fetch_document_links_for_tender(db, tender_id, *, links=None, mark_nit=True) -> FetchResult`

For each candidate URL (`nit_document_links` first, then `document_links`), apply the guard chain; on success store via `storage_service` + create a `TenderDocument` (same `(tender, source_url)` dedup as the extension upload). Never raises — total failure means "no doc fetched," pipeline proceeds on whatever exists. Returns counts (fetched / skipped-not-public / skipped-dedup / failed) + per-URL reasons, logged under the run-ID.

**Guard chain (reject → skip that URL, log, continue):**
1. **Scheme:** https only.
2. **Host allowlist:** host is (or is a subdomain of) a known portal — `ireps.gov.in`, `gem.gov.in`, `mkp.gem.gov.in`, and the aggregators from the extension's `host_permissions` (tendertiger, bidassist, tenderdetail, tendersinfo, projectstoday). Source of truth = the extension host_permissions set.
3. **SSRF/DNS guard:** resolve host; reject private/loopback/link-local/reserved IPs.
4. **Fetch:** GET, browser-like UA, connect+read timeout (~25s), capped redirects, each redirect target re-checked against the allowlist.
5. **Response checks:** status 200; document content-type (`application/pdf`/known doc types, not `text/html`); size ≤ cap (~10MB, honor `content-length` + hard read cap); first bytes match (`%PDF-`). HTML login/redirect → skip (this is how a gated link fails gracefully).
6. **Persist:** `storage_service.upload_file_sync` under the tender-doc key + `TenderDocument(source_url=url, document_type='nit' for the nit-classified link else 'other')`; trigger the same background embedding the extension upload does.

**Config (`config.py`):** `nit_link_fetch_enabled` (default OFF), host allowlist, size/timeout caps.

### 2. Trigger 1 — Eager (background, new tenders)

In the `tender_service` ingest path, after a tender is saved with a non-empty link list and no NIT `TenderDocument`, enqueue a background job (matching how `process_document_links_background` is dispatched) that calls `fetch_document_links_for_tender`. Fires once per tender (dedup + attempted-guard prevents re-scrape re-hammering). Non-blocking.

### 3. Trigger 2 — On-demand fallback (inline, analysis/scoring)

At the start of `run_document_analysis` / `_run_v2_analysis` (and the scoring entry point if independent): if the tender has zero PDF `TenderDocument`s but a stored link exists, call `fetch_document_links_for_tender` synchronously (bounded by guard timeouts), re-query docs, then proceed. If nothing fetched, proceed exactly as today (no regression).

### 4. Convergence

Extension hunt, eager backend fetch, and on-demand fallback all converge on the same `TenderDocument` table with the same `(tender, source_url)` dedup — they cooperate; the backend fetch fills the gap when the extension didn't run.

## Testing

- **Unit (no network):** guard chain — allowlist accept/reject, private-IP rejection, content-type/size/`%PDF-` checks, redirect-target re-check — with fabricated responses. Dedup + persist against the existing `TenderDocument` path.
- **Integration (opt-in, one real fetch):** a guarded fetch against a known public IREPS NIT URL → assert a `TenderDocument` lands with `source_url` + `nit` type. Skippable without network.
- **No-op safety:** analysis on a tender with an existing NIT doc must NOT fetch; analysis on a tender with no link proceeds unchanged.

## Rollout

Behind `nit_link_fetch_enabled`, **default OFF**. Enable the **on-demand fallback first** (low volume — only fires when a doc is missing at analysis time), watch logs/portal behavior, then enable **eager** (higher volume — every new tender). Staged for gov-portal load. Purely additive; no regression when off.

## Accepted risks

- A stored link could point at a stale/wrong document — bounded by the content checks; the enrichment's longer-wins/fill-if-empty rules limit downstream damage, and (out of scope here) a future agent-verify step could add corroboration.
- Portal rate limits under the eager path — bounded by the staged rollout + once-per-tender guard.
- Allowlisted portals change hosts over time — the allowlist is config, updatable without deploy.
