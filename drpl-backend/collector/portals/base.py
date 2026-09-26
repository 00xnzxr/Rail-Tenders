"""
DRPL Collector - the fetcher contract.

Every portal implements ``sweep()``, an async generator of ``Batch``. That is
the whole interface. A batch is one page's worth of work: the tenders to ship,
the progress numbers to report, and enough bookkeeping for the ledger.

Batches are yielded rather than returned so a sweep that dies at page 40 of 60
has already shipped 39 pages, and so cancellation lands on a page boundary the
fetcher chose rather than wherever a kill signal happened to arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Protocol


class ParserDrift(Exception):
    """The page parsed, but not into the shape we expect any more.

    Distinct from a network failure: nothing is down, the markup moved. Raised
    where a structural assumption fails outright (no table with the expected
    header, no CSRF token on a page that always has one). Gradual degradation
    is reported through ``parsing.drift`` instead, because a portal that
    renames one field still returns a perfectly valid page.
    """


class PortalUnavailable(Exception):
    """The portal said no in a way that retrying now will not fix.

    Sustained 403s are a signal to slow down or ask for API access, never a
    puzzle to defeat.
    """


class SessionExpired(Exception):
    """The warm session is gone and needs a fresh login (IREPS ADM.004)."""


@dataclass
class Batch:
    """One page of a sweep."""

    portal: str
    #: Tenders in drpl-backend's ``TenderInput`` shape (camelCase), ready to
    #: POST verbatim. Mapping happens in the fetcher so the sink stays dumb.
    tenders: list[dict] = field(default_factory=list)
    #: 1-based page number within the current term.
    page: int = 0
    pages_done: int = 0
    #: Rows the page contained before the ministry/scope filter.
    rows_seen: int = 0
    #: Rows dropped because they are not in scope. Tracked separately from
    #: duplicates: "not for us" and "already have it" are different answers.
    rows_skipped: int = 0
    #: The portal's own total for the current query, when it states one.
    expected_total: Optional[int] = None
    #: Distinct bid numbers this sweep has examined so far, when the fetcher
    #: tracks them. ``None`` means "not measured" and the caller must not
    #: invent a figure. It is the only honest numerator for a progress bar in
    #: ministry mode: that mode makes several convergence passes over the same
    #: pages, so pages-fetched over pages-expected sails past 100% while the
    #: distinct count is the thing actually converging.
    rows_distinct: Optional[int] = None
    #: Which search term / department / filter produced this page.
    term: Optional[str] = None
    #: e.g. "gem-allbids-v1" -- recorded per row so cross-source
    #: reconciliation can say not just that a tender was missed, but by whom.
    fetcher: str = ""
    #: Raw rows kept for drift measurement. Never shipped.
    raw: list[dict] = field(default_factory=list)

    def progress(self) -> dict:
        """The payload for a ``collect_progress`` event."""
        return {
            "portal": self.portal,
            "term": self.term,
            "page": self.page,
            "pages_done": self.pages_done,
            "rows_seen": self.rows_seen,
            "rows_skipped": self.rows_skipped,
            "in_scope": len(self.tenders),
            "expected_total": self.expected_total,
            "rows_distinct": self.rows_distinct,
        }


class Fetcher(Protocol):
    """What a portal module must provide."""

    portal: str
    version: str

    def sweep(
        self, known: set[str], params: dict[str, Any]
    ) -> AsyncIterator[Batch]:  # pragma: no cover - protocol
        """Yield batches until caught up or capped.

        ``known`` is every tender_id the ledger has already seen for this
        portal -- the incremental stop condition reads it rather than
        re-querying per row. ``params`` carries the run's own options
        (mode, terms, max_pages).
        """
        ...
