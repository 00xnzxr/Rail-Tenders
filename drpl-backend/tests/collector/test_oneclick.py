"""
The one-click run.

What matters here is the reporting contract, not the fetching -- the sweep is
covered by ``test_gem_full.py``. A one-click run is the thing a person presses
and then believes, so the summary has to be honest about three specific things:
whether the session came from Chrome, whether coverage was actually achieved,
and whether anything was shipped.
"""

from __future__ import annotations

import pytest

from collector import oneclick
from collector.oneclick import OneClickResult, build_parser, run_once
from collector.portals.base import Batch
from collector.portals.gem_full import FullSweepStats


class FakeFetcher:
    """A sweep that yields a scripted set of batches."""

    def __init__(self, batches=None, stats=None, blow_up=False):
        self._batches = batches or []
        self.stats = stats or FullSweepStats()
        self.closed = False
        self._blow_up = blow_up
        self.params = None

    async def sweep(self, known, params):
        self.params = params
        if self._blow_up:
            raise RuntimeError("portal went away")
        for b in self._batches:
            yield b

    async def aclose(self):
        self.closed = True


def _batch(*tenders) -> Batch:
    return Batch(portal="gem", tenders=list(tenders), page=1, pages_done=1,
                 rows_seen=10, fetcher="gem-full-v1")


def _tender(tid="GEM/2026/B/1", **over) -> dict:
    t = {"portal": "gem", "tenderId": tid, "title": "A tender"}
    t.update(over)
    return t


@pytest.fixture
def patched(monkeypatch):
    """Install a FakeFetcher and keep the run off the network and the ledger."""
    holder = {}

    def install(fetcher):
        holder["fetcher"] = fetcher
        monkeypatch.setattr(
            "collector.portals.gem_full.GemFullFetcher", lambda session=None: fetcher
        )
        return fetcher

    return install


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_collects_tenders_and_counts_new_ones(self, patched):
        patched(FakeFetcher([_batch(_tender("A"), _tender("B"))]))
        result = await run_once(dry_run=True, known={"A"}, use_ledger=False)
        assert result.tenders_found == 2
        assert result.tenders_new == 1
        assert [t["tenderId"] for t in result.tenders] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_closes_the_fetcher_even_when_the_sweep_fails(self, patched):
        fetcher = patched(FakeFetcher(blow_up=True))
        result = await run_once(dry_run=True, use_ledger=False)
        assert fetcher.closed is True
        assert result.errors and "portal went away" in result.errors[0]

    @pytest.mark.asyncio
    async def test_a_failed_sweep_is_never_reported_complete(self, patched):
        stats = FullSweepStats(expected_total=10)
        stats.rows_seen = 10          # coverage looks perfect...
        patched(FakeFetcher(blow_up=True, stats=stats))
        result = await run_once(dry_run=True, use_ledger=False)
        assert result.complete is False, "an errored run cannot claim coverage"

    @pytest.mark.asyncio
    async def test_complete_requires_both_coverage_and_no_errors(self, patched):
        stats = FullSweepStats(expected_total=10, final_total=10)
        stats.rows_seen = stats.rows_distinct = 10
        patched(FakeFetcher([_batch(_tender())], stats=stats))
        result = await run_once(mode="full", dry_run=True, use_ledger=False)
        assert result.complete is True
        assert result.coverage == 1.0

    @pytest.mark.asyncio
    async def test_closures_are_reported_beside_coverage(self, patched):
        # 1,000 rows at the start, 950 at the end, all 950 read: complete,
        # and the summary says where the other 50 went rather than leaving
        # a reader to wonder why expected_total and rows_seen disagree.
        stats = FullSweepStats(expected_total=1000, final_total=950)
        stats.rows_seen = stats.rows_distinct = 950
        patched(FakeFetcher([_batch(_tender())], stats=stats))
        result = await run_once(mode="full", dry_run=True, use_ledger=False)
        d = result.summary()
        assert d["complete"] is True
        assert d["coverage"] == 1.0
        assert d["expected_total"] == 1000
        assert d["final_total"] == 950
        assert d["closed_during_sweep"] == 50
        assert d["rows_distinct"] == 950

    @pytest.mark.asyncio
    async def test_dry_run_never_ships(self, patched, monkeypatch):
        patched(FakeFetcher([_batch(_tender())]))
        called = []

        async def boom(tenders):
            called.append(tenders)

        monkeypatch.setattr("collector.sink.post", boom)
        result = await run_once(dry_run=True, use_ledger=False)
        assert called == []
        assert result.tenders_shipped == 0

    @pytest.mark.asyncio
    async def test_fill_rates_report_what_actually_landed(self, patched):
        patched(FakeFetcher([_batch(
            _tender("A", estimatedValue=100.0, eligibilityCriteria="x"),
            _tender("B"),
        )]))
        result = await run_once(dry_run=True, use_ledger=False)
        # The honesty metric: two tenders, one with a value.
        assert result.details["with_value"] == "1/2"
        assert result.details["with_eligibility"] == "1/2"

    @pytest.mark.asyncio
    async def test_mode_and_details_reach_the_fetcher(self, patched):
        fetcher = patched(FakeFetcher([]))
        await run_once(mode="full", with_details=False, max_pages=7,
                       ministries=["Ministry of Coal"], dry_run=True,
                       use_ledger=False)
        assert fetcher.params["mode"] == "full"
        assert fetcher.params["with_details"] is False
        assert fetcher.params["max_pages"] == 7
        assert fetcher.params["ministries"] == ["Ministry of Coal"]

    @pytest.mark.asyncio
    async def test_session_source_is_reported(self, patched):
        patched(FakeFetcher([]))
        result = await run_once(dry_run=True, use_ledger=False)
        # A run that silently fell back would make "is it using my login?"
        # unanswerable, so the answer is always in the summary.
        assert result.session_source == "direct"

    @pytest.mark.asyncio
    async def test_chrome_failure_falls_back_rather_than_failing(
        self, patched, monkeypatch
    ):
        patched(FakeFetcher([_batch(_tender())]))

        async def fake_chrome_session(port):
            return None, "direct"

        monkeypatch.setattr(
            "collector.session.chrome.chrome_session", fake_chrome_session
        )
        result = await run_once(dry_run=True, use_chrome=True, use_ledger=False)
        assert result.session_source == "direct"
        assert result.tenders_found == 1
        assert not result.errors


class TestSummary:
    def test_summary_is_json_shaped(self):
        import json

        json.dumps(OneClickResult().summary())

    def test_errors_appear_only_when_there_are_some(self):
        assert "errors" not in OneClickResult().summary()
        r = OneClickResult()
        r.errors.append("boom")
        assert r.summary()["errors"] == ["boom"]


class TestCli:
    def test_defaults(self):
        args = build_parser().parse_args([])
        assert args.mode == "incremental"
        assert args.dry_run is False
        assert args.chrome is False

    def test_full_mode_with_chrome_and_output(self):
        args = build_parser().parse_args(
            ["--mode", "full", "--chrome", "--dry-run", "--out", "t.json"]
        )
        assert args.mode == "full"
        assert args.chrome is True
        assert args.out == "t.json"

    def test_ministries_can_be_emptied_to_disable_the_filter(self):
        args = build_parser().parse_args(["--ministries", ""])
        assert args.ministries == ""


# -- the progress line ---------------------------------------------------


class _ProgStats:
    expected_total = 1693
    rows_distinct = 0
    pages_fetched = 0
    rows_seen = 0
    rows_in_scope = 0
    recovered = 0


class _ProgBatch:
    term = None
    page = 120
    tenders: list = []


def _progress_pct(capsys, *, rows_distinct, pages_fetched):
    stats = _ProgStats()
    stats.rows_distinct = rows_distinct
    stats.pages_fetched = pages_fetched
    oneclick._print_progress(_ProgBatch(), stats)
    return capsys.readouterr().err


def test_progress_never_exceeds_100_percent_across_convergence_passes(capsys):
    """A ministry sweep reads the same pages several times.

    Pages-fetched over pages-expected showed 198.8% on the second pass of the
    first live run. Distinct rows over the portal's total is the number that
    is actually converging, and it is the one shown.
    """
    out = _progress_pct(capsys, rows_distinct=1654, pages_fetched=340)
    assert "97.7%" in out
    assert "198" not in out


def test_progress_is_capped_when_the_list_grows_under_the_walk(capsys):
    """A bid published behind the walker can push distinct past the total."""
    out = _progress_pct(capsys, rows_distinct=1700, pages_fetched=171)
    assert "100.0%" in out


def test_progress_says_nothing_rather_than_zero_without_a_total(capsys):
    stats = _ProgStats()
    stats.expected_total = None
    oneclick._print_progress(_ProgBatch(), stats)
    err = capsys.readouterr().err
    assert "?" in err and "0.0%" not in err
