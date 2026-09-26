# Pending Work & Follow-ups

Living list of deferred items, follow-ups, and next-phase work across the
features shipped to `main`. Grouped by feature. Last updated 2026-07-13.

Specs and plans for shipped work live under `docs/superpowers/specs/` and
`docs/superpowers/plans/`.

> **Update 2026-07-13 — Phases 0–2 of the backlog roadmap shipped**
> (branch `feat/pending-backlog-phases-0-2`; see
> `docs/superpowers/plans/2026-07-13-pending-backlog-phases-0-2.md`).
> Closed: the 4 failing tests (root cause was **no test isolation** — the suite
> ran against the live Neon prod DB; fixed with `tests/conftest.py` binding to a
> disposable SQLite DB; the two auth tests were corrected to assert 401 per house
> style; the relevance-sort code was already correct). NIT footer-artifact filter
> generalized; escalation-free costing fallback fixed. Segment-population pinned
> by a characterization test; the prod backfill and both verification gates are
> now **human-gated runbooks** (`docs/RUNBOOK-segment-backfill.md`,
> `docs/RUNBOOK-phase3-verification.md`). Still open: the **Pydantic v2 migration**
> (deferred to its own PR — mechanical, ~20 files) and all Phase 4+ net-new
> features (labour wages, source links). Also note: `pymupdf>=1.24.0` (declared in
> `requirements.txt`) was missing from the local venv — install it or 4 costing
> tests are uncollectable.

---

## 1. Tenders funnel redesign (shipped)

**What shipped:** score-tiered funnel (Pursue ≥70% / Review 40–70% / Set aside
<40% / New unscored) as top-of-page tabs, an always-visible FilterBar, and a
relabeled "Select" bulk-action mode.

**Follow-ups:**
- **Stored `segment` is unpopulated.** **PARTIALLY ADDRESSED (Phase 1,
  2026-07-13).** `resegment_all()` + its admin endpoint already existed; the
  behavior is now pinned by a characterization test
  (`tests/test_segment_backfill.py`). The actual **prod backfill mutates live
  Neon and is human-gated** — run it via `docs/RUNBOOK-segment-backfill.md`
  (`PUT /api/admin/tender-scoring/settings?resegment=true`). The hybrid rule
  (score-based piles respecting a manual `segment` override) is the documented
  design; the override **UI** is still to build.
- **Manual override into Pursue.** No UI to drag a low-score tender the user
  likes into Pursue (would need the hybrid model above).

---

## 2. NIT-faithful costing (Piece 1 of 3 — shipped)

**What shipped:** deterministic IREPS NIT parser, strict dedup, reconciliation
gate (`needs_review`, never fabricates), two separated number-worlds (locked NIT
vs firm estimate), fabrication lockdown. Verified exact on Tender #2531
(60,879,392.16).

**Follow-ups (deferred minors):**
- ~~**Boilerplate footer filter is a fixed IREPS-string enumeration.**~~
  **ADDRESSED (Phase 2a, 2026-07-13).** `_BOILERPLATE_RE` generalized to catch
  page-number variants ("Page 4", "4 | Page", "Page | 4", "- 7 -") while keeping
  the specific strings and not matching real description lines. Reconciliation
  gate remains the safety net.
- ~~**`normalize_copied_rates` escl-free fallback.**~~ **FIXED (Phase 2b,
  2026-07-13).** The fallback now routes through the escalation-inclusive formula
  (`tender_rate × qty × (1 + escl/100)`), consistent with the authoritative path.
- **Live full-run smoke not done.** The exact NIT fidelity is proven by the
  automated E2E and a live read-only parse of the production NIT, but a full
  LLM costing run → downloaded XLSX for #2531 was not executed (prod file in R2
  the local env can't reach; a real run needs the worker + would mutate prod).
  Worth a manual UI run to eyeball the generated sheet + reconciliation block.

**Next phases (own spec → plan → build each):**
- **Piece 2 — Labour wage sourcing:** cost labour from Indian government
  minimum-wage schedules (railway/construction/maintenance) by region, and
  surface which wage / state / parameter was used.
- **Piece 3 — Source links:** capture and show the OEM / IndiaMART / website URL
  behind each costed part so users can contact suppliers directly.

---

## 3. Eager tender analysis (Piece A of 3 — shipped, OFF by default)

**What shipped:** auto-runs the existing v2 analyzer for in-scope tenders
(score ≥ 0.70 or eligible-flagged) that have documents, via a self-rescheduling
sweep with fail-closed backpressure, a daily cap, idempotency, a per-tender
status marker + orphan recovery, and a token-owned Redis slot lock enforcing
`max_concurrency=1`. Master flag `eager_analysis_enabled=False`.

**Before enabling (`eager_analysis_enabled=True`):**
- **Live smoke test.** Needs a running RQ worker + Redis. Enable the flag on a
  worker and confirm a real in-scope tender with documents gets auto-analyzed
  (status `queued`→`running`→`done`, a `DocumentExtractionResult` created), and
  that backpressure/daily-cap behave. The automated tests stub the queue.

**Follow-ups (deferred minors):**
- **Orphan-reaper cutoff (30 min)** must exceed the worst-case single-tender
  analysis time; a genuinely long analysis interrupted by a web restart could be
  re-selected. Consider making the cutoff configurable and comfortably large.
- **Daily-cap under-count** if the per-tender `_set_status("queued")` commit
  fails after a successful enqueue — the counter isn't incremented and the tender
  is re-selected next tick. Idempotency (`_already_analyzed` re-check +
  deterministic `job_id`) makes the re-run safe; it's a cap-accounting
  inaccuracy, not a correctness bug.
- **Bootstrap not gated on the flag** (cosmetic): the sweep is always
  bootstrapped and no-ops internally when disabled — a one-line comment would
  preempt confusion.

**Next phases (own spec → plan → build each):**
- ~~**Piece B — Extension gated auto-capture.**~~ **SHIPPED (2026-07-13, flag-OFF)**
  — spec `docs/superpowers/specs/2026-07-13-extension-gated-auto-capture-design.md`,
  plan `…/plans/2026-07-13-extension-gated-auto-capture.md`. Local strict scope
  pre-filter (`utils/scope-matcher.ts`: keyword AND ministry AND value, fail-closed
  but records `missing[]`) auto-runs the existing document hunt for the capped
  in-scope subset when `features.auto_capture` is ON (default OFF), sequential +
  throttled, with a concurrency guard. Smoke: `docs/RUNBOOK-extension-auto-capture.md`.
  **Before enabling the flag:** run the smoke runbook. **Deferred cosmetic minors**
  (final review, non-blocking, dormant while OFF): redundant `settings` shadow in
  `extension.py:get_extension_config`; `sourcePortal` folded into the ministry
  haystack (intended). Until B was enabled, Piece A only analyzed docs the *manual*
  hunt uploaded.
- **Piece C — Metadata reconciliation:** clean the DOM-scraped list fields
  (title / value / dates / department) from the doc-extracted truth, plus a cheap
  metadata-normalization piggyback on the eager scoring call.

---

## 4. Cross-cutting / repo hygiene

- ~~**Pre-existing test failures (4).**~~ **FIXED (Phases 0a/0b, 2026-07-13).**
  Root cause was **no test isolation** — the suite bound to the live Neon prod DB
  (`.env`), so injected rows fell outside `limit=200`. Fixed with
  `tests/conftest.py` (disposable SQLite). The auth tests now assert 401 (house
  style); the relevance-sort production code was already correct. Full suite:
  169 passed.
- **Pydantic v2 deprecation warnings** repo-wide (`class Config` → `ConfigDict`),
  e.g. `app/core/config.py:10`. Noisy in test output; a mechanical migration.
  **Still open — deferred to its own PR** (out of scope for Phases 0–2; ~20 files,
  zero behavior change, kept off the fix branch to avoid noise).
- **Backend restart note.** When run for verification it was launched as plain
  `uvicorn ... --port 8000` (no `--reload`) because the `--reload` watcher proved
  unreliable on the Windows dev host. For live editing, restart with `--reload`
  yourself. The two migrations (costing reconciliation columns, eager-analysis
  status columns) self-heal on startup.
