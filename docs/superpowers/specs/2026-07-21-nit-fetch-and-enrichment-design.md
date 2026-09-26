# NIT Auto-Fetch + Tender Field Enrichment — Design

**Date:** 2026-07-21
**Status:** Approved (brainstorming) — pending implementation plan

## Problem

In the tender analysis pipeline, the analysis agent "cannot provide bid value, EMD amount, and closing date properly," and the tender title stays truncated. The user wants the agent to open each tender's NIT document link, extract these fields (plus the full-length title and scope of work) reliably, and update the tender with them.

## Root cause (confirmed by code map)

The analyzer **already extracts** all five fields — its per-doc `PER_DOC_SYSTEM_PROMPT` (`document_analysis_agent.py:596`) captures `commercials.advertised_value`, `commercials.emd`, `commercials.closing_date_raw`, `commercials.name_of_work`, and `key_facts.scope_summary` (verbatim from the PDF). The failure is two structural gaps:

- **Gap A — the NIT PDF is often never fetched.** The extension scrapes `Tender.nit_document_links` / `document_links` (`tender_service.py:144`, `:124`), but **no backend code ever downloads them** — they are write-only metadata. The analyzer (`_run_v2_analysis`, `document_analysis_agent.py:1645`) only reads PDFs that already exist as `TenderDocument` rows (from a manual extension upload or GEM auto-fetch). So for most scraped tenders the agent has no NIT to read and guesses from thin listing text.
- **Gap B — extracted values are never persisted to the Tender row.** The only bridge, `enrich_tender_from_analysis` (`tender_enrichment_service.py:192`), is gated to `command_center`-created tenders only (`:209-219`) and maps just `title`/`tender_id`/`organisation` — it never writes `estimated_value`, `emd_amount`, or `closing_date` for any tender, scraped or not.

## Decisions (from brainstorming)

- Fix **both** gaps end-to-end.
- NIT fetch happens **in the extension** (it holds the authenticated DSC/portal session; a plain backend GET would hit a login page for gated portals).
- Trigger: **during scrape when a tender's document area is found** (Section 2 default: on tender select/open, not for every row of a bulk listing scrape — avoids portal rate limits; flagged for override).
- Fetch **all documents on the tender's page** (NIT + BOQ + drawings + special conditions + corrigenda + annexures), NIT flagged.
- Write-back: **NIT wins (overwrite) for title + scope**; **fill-if-empty for bid value / EMD / closing date** (never clobber a good scraped number with an AI mis-read).
- Verbatim numeric/date strings **parsed defensively**; unparseable → column left unchanged + logged (the verbatim value still appears in the analysis report).

## Non-goals

- No change to the analyzer's extraction prompts (they already capture the fields).
- No backend-side authenticated portal fetching.
- No destructive migration; all changes additive.

## Gap A — Extension document auto-fetch

> **Implementation reality (verified against current code):** the extension
> **already** does the download+upload. `processDocumentHunt`
> (`service-worker.ts:346`) opens each tender's `detailUrl` (viewNIT page) in a
> background tab, extracts ALL `classifiedDocuments`, and calls
> `downloadAndUploadDocument` (`:873`) for each — authenticated `fetch()` in
> portal context, HTML/>10MB skip, stable dedup filenames — which uploads via
> `uploadDocument` (`api-client.ts:194`) → `POST /api/extension/documents`
> (`extension.py:218`). It already passes `document_type` (incl. the `nit`
> classification) and `portal`. There are two triggers today: the gated
> **auto-capture** path (`service-worker.ts:281-324`, scope-filtered, flag
> `extension_auto_capture_enabled`, see [[piece-b-extension-auto-capture]]) and
> the **manual "Hunt Documents"** button (`:1399`).
>
> **So Gap A is ~90% built.** The remaining deltas are small:
> 1. **Thread `source_url`** (`doc.url`) through `downloadAndUploadDocument` →
>    `uploadDocument` (add `source_url` to the FormData) → `extension.py`, so the
>    backend can (a) dedup by `(tender_id, source_url)` and (b) let Gap B prefer
>    the NIT-flagged doc. Currently `doc.url` is NOT sent (only `document_type`).
> 2. **Backend:** persist `source_url` on `TenderDocument.source_url` (column
>    already exists); dedup on `(tender_id, source_url)`; keep `document_type`
>    so `is_nit = (document_type == 'nit')` is derivable.
> 3. **Trigger for the user's ask** ("fetch when I open a tender, broader than the
>    strict scope filter"): reuse the existing manual-hunt entrypoint or relax the
>    auto-capture gate — no new download machinery.
>
> The canonical flow below is therefore mostly documentation of what exists; the
> plan implements only deltas 1–3.

**Flow (per scraped tender with a document area):**
1. Extension collects the full document list on the tender's page, preserving each link's classified `type` (from `classifyDocumentLink`, `ireps.ts:704`) and title; the NIT-classified link is flagged.
2. For each link, the extension does an authenticated `fetch()` **in the portal page context** (carries DSC session cookies) to get the file bytes.
3. It POSTs each file to the backend as a `TenderDocument` (extends the existing `extension.py` upload route), passing `source_url`, `file_name`, classified `type`, and an `is_nit` flag.
4. Backend stores via `storage_service` exactly like a manual upload, so `_run_v2_analysis` picks the docs up automatically (it iterates `TenderDocument` rows ending in `.pdf`).

**Guards:**
- **Dedup by `source_url` per tender** — re-scraping doesn't re-download/re-store existing docs.
- Skip non-PDF/non-document links (best-effort content-type check).
- Per-document failures are non-fatal + logged; the tender still analyzes on whatever downloaded.
- **Per-tender doc cap** (configurable): over the cap, prioritize NIT + BOQ/price-schedule, log the rest as skipped.
- Content-script gathers links; the **service worker** performs download + upload (only it talks to the backend).

**Open defaults (override-able):**
- (a) Fetch on tender **select/open**, not automatically for every row in a bulk listing scrape (portal rate-limit safety).
- (b) Keep the per-tender doc cap with NIT+BOQ prioritized.

**Rollout:** behind a config flag, default OFF until validated against live portal rate limits, then flip ON.

## Gap B — Persist NIT-extracted values to the Tender

Extend `enrich_tender_from_analysis` (remove the `command_center`-only guard so it runs for all tenders) or add a sibling enrichment invoked from `_run_v2_analysis` after the summary write.

**New pure helpers (deterministic, unit-tested):**
- `_parse_indian_currency(s) -> Optional[float]` — `₹ 1,12,55,870.40`, `Rs. 2,25,000/-`, `INR 45.6 Lakh`, `2.5 Cr`, bare numbers; `None` on ambiguity.
- `_parse_tender_date(s) -> Optional[datetime]` — `21/07/2026 15:00 hrs`, `21-Jul-2026`, ISO, common IREPS/GeM formats; `None` on ambiguity.

**Mapping** (reads the analyzer's existing `commercials` + `key_facts`; picks the most authoritative doc via existing `_pick_primary_facts`, preferring the `is_nit`-flagged doc):

| Tender column | Source | Write rule |
|---|---|---|
| `title` | `key_facts.name_of_work` / `scope_summary` (full) | Overwrite (NIT wins), unless the NIT value is corrupt/empty (`is_corrupt_title`) |
| `description` | `key_facts.scope_summary` | Overwrite (NIT wins) |
| `estimated_value` | `commercials.advertised_value` → `_parse_indian_currency` | Fill-if-empty; skip on parse fail |
| `emd_amount` | `commercials.emd` → `_parse_indian_currency` | Fill-if-empty; skip on parse fail |
| `closing_date` | `commercials.closing_date_raw` → `_parse_tender_date` | Fill-if-empty; skip on parse fail |

Every write and every skip (parse fail / non-empty numeric) logged under the run-ID with the verbatim source string.

**Rollout:** additive, no flag.

## Testing

- **Unit (pure):** currency + date parsers against a table of real IREPS/GeM shapes (lakh/crore, `/-` suffix, `hrs` time, garbage → `None`); the mapping function with a fabricated `commercials`/`key_facts` fixture asserting overwrite-vs-fill-if-empty.
- **Gap A:** scrape one real tender → assert N `TenderDocument` rows land with `source_url`/`is_nit`; re-scrape → no duplicates.
- **Gap B:** run analysis on a tender with a known NIT → assert title/scope overwritten, numerics filled-if-empty, unparseable skipped.

## Accepted risks

- An AI mis-read of the full title could overwrite a correct short title — bounded by `is_corrupt_title` and by the NIT being the authoritative source the user explicitly wants.
- A tender with many large attachments could slow the scrape — bounded by the per-tender doc cap.
- Portal rate limits under bulk fetching — bounded by the on-select trigger + config flag.
