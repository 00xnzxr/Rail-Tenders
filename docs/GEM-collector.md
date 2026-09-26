# GeM collection

One click, live data, every field the tender card shows.

```bash
cd drpl-collector
pip install -r requirements.txt

python -m collector.oneclick --mode full --dry-run --out tenders.json
```

No token, no database, no Redis, no browser. It walks the live portal, keeps the
Ministry of Railways bids, opens each one's bid document, and writes out rows
carrying the value, the EMD, the ePBG, the eligibility block, the consignee and
the delivery terms.

---

## The three modes are three different promises

| | `--mode incremental` | `--mode ministry` | `--mode full` |
|---|---|---|---|
| Query | search terms, newest first | GeM's own ministry filter, repeated until it converges | the whole list, oldest first, then the terms to the end |
| Stops | after 30 already-known bids | when distinct rows reach GeM's count for the ministry | at the last page |
| Listing pages | ~45 | **~510** (171 × 3 passes) | 4,773 |
| Requests, total | ~440 | **2,208** | 6,476 |
| Time, with documents | about a minute | ~13 minutes | ~12 minutes |
| Time, `--no-details` | seconds | **~40 seconds** | ~10 minutes |
| Reports coverage | no | **yes** — against GeM's own count for that ministry | **yes** — against the portal's final total, plus what the term pass recovered |
| Use it for | the Search button | routine runs, hourly | the first run, and nightly |

`incremental` answers *what is new?* It cannot see a back-dated publication or a
status change, because it stops before reaching them. That is the right trade
for a button. It does **not** report coverage, and the UI says so rather than
letting 23% of the corpus read as all of it.

`ministry` asks the portal to do the filtering. Same tenders, **89% fewer
listing requests** — see the section below for why it needs more than one pass.

Be clear about what that buys, because the measured wall clock is *not* it: a
run that reads every bid document is bound by ~1,700 PDF fetches, and both
modes do those. Ministry mode finished in 13.1 minutes against full's 12.3. The
win is **load**: 2,208 requests against 6,476. That is what lets this run hourly
instead of nightly without the portal noticing, and it is the whole difference
when documents are off.

`full` is the one that audits the *whole corpus* rather than one ministry's
slice of it, and it is the only mode that would notice a railway tender GeM's
own ministry field has mislabelled. Run it first, and run it nightly.

---

## What was measured against the live portal (2026-09-12)

**The whole corpus is walkable.** `POST /all-bids-data` with an empty search
returns every live bid and paginates to the end: `numFound` 47,246, page 4,725
is the last. Latency per page is 0.2–0.6 s at best and up to 2.5 s on the same
afternoon, which is why the walk keeps a rolling set of pages in flight rather
than fetching them in gathered windows — a window is as slow as its slowest
request.

**Keyword search does not miss tenders for vocabulary reasons.** A 7,000-bid
slice of the unfiltered list held 163 Ministry of Railways bids; a complete
sweep of the configured terms found all 163. GeM's full-text index covers the
buyer's organisation name, so `railway` reaches bids for multimedia projectors
and cleaning contracts alike. This is worth stating because it is the obvious
theory and it is wrong.

**The page cap does miss tenders, badly.** `gem_max_pages_per_term` is 40 — 400
rows — and the terms return far more:

| term | numFound | pages | reachable at 40 pages |
|---|---|---|---|
| railway | 1,812 | 182 | 400 |
| indian railways | 2,351 | 236 | 400 |
| AMC | 933 | 94 | 400 |
| annual maintenance | 839 | 84 | 400 |

Distinct Ministry of Railways bids reachable on a first run with the cap: **468**.
Without it: **1,788**. The cap alone puts **1,320 live railway tenders — 73.8% —
out of reach**, silently, because a sweep that stops at its own cap looks exactly
like one that finished.

That is what `--mode full` fixes, and why coverage is reported as a number.

**A full walk, measured end to end** — the run this pipeline is built to make:

| | |
|---|---|
| pages walked / failed | 4,708 / **0** |
| rows examined (distinct) | 46,954 of 47,128 — **coverage 0.9963** |
| railway tenders | **1,779**, of which 4 recovered by the term pass |
| bid document read | 1,777 |
| elapsed | **16m 43s** |
| `complete` | **true** |

A term sweep of the whole ministry taken straight afterwards found **0** live
tenders the walk had not: 1,779 against 1,779. That is the claim this system
exists to make, and it is checked rather than asserted.

Field fill, against the same run: eligibility 99.9%, delivery 99.9%, EMD amount
99.2%, estimated value 46.8%, ePBG 44.6%. The low ones were sampled against
their PDFs — in every case the document does not state the field. GeM lets a
buyer withhold the estimate, and an ePBG is not always required.

**The detail stage was serialising the walk.** Run inline per page, it parked
the page walk for as long as each page's PDFs took to read. On the same 300
pages: 129.5 s inline, 98.1 s with the reads pipelined behind the walk — and
that slice carried only 41 documents. Over the whole ministry it is the
difference between the walk and the walk plus every PDF.

**GeM serves about four requests at a time per session cookie** and queues the
rest: six concurrent pages on one session, four back in 0.5 s and two in 1.6 s;
the same six on six sessions, all back in 0.5 s. So the walk spreads its pages
over a small pool of sessions and the PDF reads get a session of their own.
Same 300 pages, listing only: 67.8 s on one session, 53.0 s on four — the rest
of each slot's time is the politeness pause, which is the point.

---

## The portal does have a ministry filter, on another endpoint

For a long time this document said GeM has no ministry filter. What is true is
that `/all-bids-data` has none. The portal's own **Advanced Search** page calls
a different endpoint, `POST /search-bids`, which takes
`searchType: "ministry-search"` and a ministry name, answers an anonymous
request, and returns the same Solr envelope. Measured 2026-09-22:

| | |
|---|---|
| `numFound` for "Ministry of Railways" | **1,703** |
| live bids on the portal at the time | 43,770 |
| pages to enumerate | **171**, against 4,378 for the walk |

The value it takes is the ministry name spelled exactly as
`ba_official_details_minName` spells it, so the filter and the scope check
agree by construction rather than by luck.

**It pages unstably, and that is the whole design constraint.** `/search-bids`
ignores the sort key, so its result order is not fixed between requests and one
pass under-delivers. Measured over eight consecutive passes:

| pass | distinct bids | new |
|---|---|---|
| 1 | 1,405 | +1,405 |
| 2 | 1,628 | +223 |
| 3 | 1,684 | +56 |
| 4 | **1,703** | +19 |
| 5–8 | 1,703 | +0 |

A single pass would have shipped 1,426 of 1,703 and looked exactly like a
complete one — the same failure shape as the 40-page cap this project was
built to remove. So `--mode ministry` is written as *repeat until the distinct
count reaches the portal's own `numFound`*, never as *read every page once*.
The denominator belongs to GeM, so the mode cannot quietly under-collect: it
either reaches that number and says `complete: true`, or it stops short and
reports coverage below 1. `gem_ministry_settle_passes` stops it early if the
count turns out to be unreachable, and that case reports partial rather than
rounding up.

**Repeating passes costs listing pages, not documents.** The obvious objection
is that four passes means four times the work. It does not: the detail stage is
driven by the tenders a page contributed that the run had not already seen, so a
bid read on pass 1 is recognised as a duplicate on pass 2 and its PDF is never
fetched again. Four passes over 171 pages is ~680 listing requests against the
walk's 4,773 — and the same ~1,700 documents either way, which is what the run's
wall clock is actually made of.

**The mode, measured end to end on 2026-09-22:**

| | |
|---|---|
| convergence passes | **3** (1,351 -> 1,654 -> 1,693) |
| listing pages / failed | 510 / **0** |
| rows examined (distinct) | 1,693 of 1,693 - **coverage 1.0000** |
| requests, total | **2,208** against the walk's 6,476 |
| elapsed | 13m 6s |
| `complete` | **true** |

Checked against two independent enumerations of the same ministry taken the
same day - the 4,378-page walk, and a `railway` term sweep read to its end -
all three agree on the same set.

`full` still exists, and not out of nostalgia. It is the only mode that reads
the whole corpus, so it is the only one that could ever catch a railway tender
whose `minName` GeM has filled in wrongly — the ministry filter would never
show it, by definition. Cheap mode hourly, audit nightly.

---

## The listing's "Z" is Indian Standard Time

`final_start_date_sort` and `final_end_date_sort` come back looking like UTC:

    "final_start_date_sort": ["2026-09-19T22:00:00Z"]

The portal's own UI shows that same bid starting at **19-09-2026 10:00 PM**.
Same wall clock, so the digits are IST and the `Z` is decoration.

Read literally it puts every deadline 5h30m late. Sampling 40 live bids and
comparing each listing date against that bid's own PDF — which `gemdoc` has
always converted correctly:

| | before | after |
|---|---|---|
| listing matched the document exactly | **0 / 40** | **35 / 40** |
| offset by exactly +5h30m | 35 / 40 | 0 / 40 |

The remaining 5 differ by whole days in both columns: those are bids a
corrigendum has since extended, where the **listing is right and the document
is stale**. That is also why the listing stays authoritative and the detail
stage only fills a gap — `_set` is fill-if-empty, which is what makes that
safe.

`gem.ist_instant` does the conversion, once, on the raw listing field. It is
deliberately not idempotent: a real `+00:00` and GeM's counterfeit one are
byte-identical, so applying it twice would shift by another 5h30m. A test pins
that, as a warning rather than an endorsement.

---

## Sort order is the bug nobody sees

Paginating a live list is lossy whenever a row the walker has already passed
*disappears*: every row behind it moves up one place, the row that was first on
the next page is now last on the page just read, and it is skipped — with no
gap, no error, and a `numFound` that still looks right.

Three orders were measured, each against a term sweep of the whole ministry
taken straight after the walk:

| order | live railway tenders the walk never saw |
|---|---|
| `Bid-Start-Date-Latest` | not walkable — every new bid shifts the rows under the walker |
| `Bid-Start-Date-Oldest` | **4 of 1,785**, all published days earlier: rows that moved up when 185 bids closed under the walk |
| `Bid-End-Date-Latest` | **287 of 1,779** |

The end-date order looked right on paper — the bid closing soonest is the last
row, so a closure leaves from the tail and shifts nothing the walker has read.
It lost 16% in practice. End dates cluster at round times, so that order has
tie groups thousands of rows wide, and GeM re-indexes a bid whenever anything
about it changes, which moves it to the end of its tie group and shifts every
row between. Start dates are unique to the second, so under the oldest-first
order a re-index moves a row within a group of one or two and nothing crosses a
page boundary.

So `full` walks **oldest-start-first**, and its one remaining loss — a row that
moved up because a bid behind the walker closed — is closed by a second, cheap
pass: it then walks the configured search terms **to the end, without stopping
on known ground**, and every in-scope tender that pass finds which the
enumeration did not is reported as `recovered`. About 420 pages. It is the
enumeration's audit as much as its safety net: when `recovered` stays at a
handful, the walk is doing what this section says it does.

Two consequences for the numbers you see:

- **Coverage is distinct rows the enumeration examined over the portal's final
  total**, so neither a row seen twice nor a bid that closed mid-walk can flatter
  or penalise it. The first version divided by the *starting* total and called
  the 18 empty pages at the end of a shrunken list "failed"; they were not.
- **A page that fails is still a failure**, counted separately, and one is
  enough to make the run report `complete: false`. Nothing about the sort
  order or the repair pass softens that.

`incremental` keeps newest-first, where the drift is harmless — it stops after a
few pages, so a shifted row is at worst seen twice.

---

## Where the eligibility criteria come from

GeM's listing endpoint returns 34 fields and not one is a price, an EMD, a
qualifying condition or a delivery term. All of it is in the bid PDF, which
`bidplus.gem.gov.in/showbidDocument/{b_id}` serves to anyone.

`collector/gemdoc/` reads that PDF into stated facts — deterministically, not
with a model:

```python
from collector.gemdoc import parse_bid_document

detail = parse_bid_document(pdf_bytes)
detail.estimated_value                  # 8533728.0
detail.emd_amount                       # 170680.0
detail.epbg_percentage                  # 5.0
detail.min_average_annual_turnover_text # '43 Lakh (s)'
detail.years_of_past_experience         # '3 Year (s)'
detail.documents_required_from_seller   # ['Experience Criteria', 'Bidder Turnover', ...]
detail.eligibility_text()               # the block the tender card renders
```

The document is machine-generated from a fixed bilingual template, so parsing it
beats summarising it on every axis that matters: **$0 instead of ~$37 a sweep**,
milliseconds instead of an API round trip, the same answer every time, and GeM's
own words (`Yes | Complete`, `3 Year (s)`) instead of a paraphrase.

The AI stage in `collector/enrich/` keeps a real job — scanned documents, free
text with no fixed home, and the day the template changes. `needs_ai_fallback`
is how the parser asks for it instead of quietly returning a thin result.

Three things it gets right that are easy to get wrong:

- **A reverse auction is followed to its parent bid.** An RA document is one page
  and states almost nothing; the bid it came from has the terms. Reading the RA
  alone produces a tender that looks fetched and is empty.
- **"Not stated" is not zero.** A field GeM omits stays `None`. `EMD Required: No`
  becomes `0`, because that is a fact about the bid.
- **A failed parse enriches nothing** rather than half of something, and leaves
  `isDetailExtracted` false so DRPL's own pipeline knows there is work left.
- **A service bid has no delivery days.** Its consignee table names a reporting
  officer, and the engagement is stated once in the header as `Contract
  Period: 2 Year(s) 1 Day(s)`. That was 712 of the 1,782 railway tenders on the
  live sweep, every one shipping with a blank timeline until the label was read.

---

## Using a logged-in Chrome

Nothing above needs a login — every endpoint the collector reads answers an
anonymous client, verified live. The browser bridge exists for the pages that do
need one, and as insurance if GeM's WAF ever starts demanding a real handshake.

```bash
python -m collector.session.chrome --login   # sign in to GeM once, leave it open
python -m collector.session.chrome --check   # confirm a session can be borrowed
python -m collector.oneclick --chrome        # scrape through it
```

To use the login you already have, in your everyday Chrome profile:

```bash
python -m collector.session.chrome --login --system-profile
```

Chrome has to be **fully closed** first — every window, and check the tray. It
will not open a DevTools port on a profile another process is holding, and a
second launch against a live profile quietly hands the URL to the running
instance and exits, leaving no port and no error. The command checks and tells
you rather than appearing to fail. Your GeM login is not affected; it lives in
the profile, not the window.

It attaches over the DevTools protocol to a Chrome started against a profile the
collector owns, takes that browser's GeM cookies, and hands them to the ordinary
httpx pipeline — so the sweep runs at httpx's speed on the browser's session,
with no browser automation in the hot path.

If Chrome is not there, is not logged in, or has no DevTools port, the run falls
back to a direct session and says so in `session_source`. A missing browser is
never the reason a sweep did not happen.

**It does not read Chrome's cookie database.** That would mean the profile's
master key, every cookie for every site rather than the one host we asked about,
breakage whenever Chrome changes its at-rest encryption, and behaviour
indistinguishable from credential-stealing malware. Attaching to a port the user
deliberately opened is explicit and scoped.

---

## In the platform

`gem_full` is a portal name like any other, so the existing route, queue, run
stream and Stop button work unchanged:

```bash
curl -X POST "$DRPL_API_URL/api/collect/runs" \
  -H "Authorization: Bearer $DRPL_SERVICE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"portals":["gem_full"],"mode":"full"}'
```

It returns a `run_id` immediately; stream it with `GET /api/runs/{id}/events`.

A full sweep can exceed the 30-minute job timeout once the portal grows. Split it
by raising `GEM_CONCURRENCY` or running `full` nightly and `incremental` during
the day — never by raising the timeout, because the SSE endpoint has its own
40-minute wall clock and the stream would close on a job still running.

---

## Settings that matter

| | default | |
|---|---|---|
| `GEM_CONCURRENCY` | 6 | pages in flight during enumeration, at all times |
| `GEM_PAGE_DELAY_SECONDS` | 0.4 | each slot rests this long after its page |
| `GEM_FETCH_DETAILS` | true | off = listing rows only, no eligibility |
| `GEM_DETAIL_CONCURRENCY` | 4 | bid documents in flight, sweep-wide |
| `GEM_DETAIL_LOOKAHEAD` | 16 | batches whose documents may still be reading while the walk goes on |
| `GEM_SESSIONS` | 4 | independent portal sessions: one for the documents, the rest for the walk |
| `GEM_FULL_REPAIR_PASS` | true | walk the terms to the end after a full enumeration |
| `GEM_MAX_PAGES_PER_TERM` | 40 | backstop for `incremental` only |
| `TARGET_MINISTRIES` | Ministry of Railways | matched on GeM's structured field |

---

## Tests

```bash
python -m pytest tests/test_gemdoc.py tests/test_gem_full.py tests/test_gem_detail_stage.py
```

The `gemdoc` fixtures are the extracted text of real PDFs pulled from the live
portal, not hand-written samples. That matters: almost every bug the parser had
came from a shape nobody would have invented — the reverse-auction template
printing labels with no slash in front of them, GeM omitting the "EMD Required"
line on the bids that actually charge one, a PIN code sitting where the delivery
period goes.
