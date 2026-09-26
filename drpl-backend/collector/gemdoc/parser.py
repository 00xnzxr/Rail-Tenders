"""
DRPL Collector - parsing a GeM bid document into stated facts.

The output of this module is ``GemBidDocument``: everything the bid PDF states,
typed, with "the document does not say" represented as ``None`` and never as a
zero, an empty string, or a guess. That distinction is the whole point. DRPL's
tender card shows ``EMD --`` for both "no EMD required" and "we did not look",
and only one of those should stop somebody opening the PDF themselves.

WHY A PARSER AND NOT A MODEL
----------------------------
The collector already has an AI enrichment stage (``collector/enrich``) that
reads the same PDF with Claude and returns the same fields. It works. This
parser exists because, for *this* document, it is strictly better on all four
axes that matter:

  * **Cost.** Around $0.023 per document, measured. A full Ministry-of-Railways
    sweep is roughly 1,600 documents, so ~$37 a sweep versus $0.
  * **Latency.** An API round trip per tender against a few milliseconds.
  * **Determinism.** The same PDF gives the same answer every time, which is
    what makes a diff between two sweeps mean something.
  * **Fidelity.** GeM states ``MSE Relaxation ... Yes | Complete`` and
    ``ePBG Percentage 3``. A summariser paraphrases those; this returns them.

The model keeps a real job: free-text fields with no fixed home in the template
(a scope summary), documents that are scanned rather than generated, and the
day GeM changes the template. ``needs_ai_fallback`` is how this module says so
out loud instead of silently returning a thin result.

VERIFIED AGAINST 15 LIVE DOCUMENTS (2026-09-12)
-----------------------------------------------
5 bid documents and 10 reverse-auction documents pulled from the live portal,
covering BOQ and category bids, single- and two-packet, MSE- and MII-preference,
with and without an estimated value. The reverse-auction form is genuinely
different -- one page, no EMD/ePBG block, ``Exemption`` where a bid says
``Relaxation`` -- and it names its parent bid, which is where its real terms
live. ``parent_bid_number`` carries that so the fetcher can follow it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from collector.gemdoc.fields import Found, Kind, find_labels
from collector.gemdoc.normalize import (
    NormalizedDocument,
    extract_links,
    is_junk_line,
    page_count_of,
)

__all__ = ["Attachment", "GemBidDocument", "Consignee", "parse_bid_document",
           "parse_text"]


#: How a URL found in the bid document is classified. The path tells you what
#: the file is, and GeM's paths are stable enough to rely on: buyer uploads go
#: under /resources/upload_nas/.../biddoc/ and /excel/, the service-level
#: agreement under fulfilment.gem.gov.in, the general terms under admin.
_LINK_KINDS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"/resources/upload_nas/.+/excel/", re.I), "boq"),
    (re.compile(r"/resources/upload_nas/.+/biddoc/", re.I), "buyer_document"),
    (re.compile(r"/showbidDocument/|/showradocumentPdf/", re.I), "bid_document"),
    (re.compile(r"fulfilment\.gem\.gov\.in/contract/slafds", re.I), "sla"),
    (re.compile(r"admin\.gem\.gov\.in/apis/v1/gtc/", re.I), "gtc"),
    (re.compile(r"/bidding/bid/showCatalogue/", re.I), "catalogue"),
    (re.compile(r"/bidding/bid/bidsla/", re.I), "sla"),
    (re.compile(r"/bidding/downloadOmppdfile/?$", re.I), "skip"),
)


@dataclass
class Attachment:
    """A file the bid document links to."""

    url: str
    kind: str = "other"

    @property
    def filename(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def classify_links(urls: list[str]) -> tuple[list["Attachment"], list[str]]:
    """Split raw link annotations into attachments and mailto addresses.

    ``skip`` drops the bare endpoints GeM templates in with no file behind
    them -- ``/bidding/downloadOmppdfile/`` appears on every document and
    always 404s, so shipping it as a document link means every tender arrives
    carrying one broken attachment.
    """
    attachments: list[Attachment] = []
    addresses: list[str] = []
    for url in urls:
        if url.lower().startswith("mailto:"):
            addr = url[7:].split("?")[0].strip()
            if addr and addr not in addresses:
                addresses.append(addr)
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        kind = "other"
        for rx, name in _LINK_KINDS:
            if rx.search(url):
                kind = name
                break
        if kind == "skip":
            continue
        attachments.append(Attachment(url=url, kind=kind))
    return attachments, addresses


# ---------------------------------------------------------------------------
# Value coercion. Each returns None rather than a default when the text does
# not actually state the thing -- see the module docstring.
# ---------------------------------------------------------------------------

_MULTIPLIERS = (
    (re.compile(r"\bcrore?s?\b", re.I), 10_000_000),
    (re.compile(r"\blacs?\b|\blakh?s?\b", re.I), 100_000),
    (re.compile(r"\bthousands?\b", re.I), 1_000),
)

_NUMBER = re.compile(r"(\d[\d,]*\.?\d*)")


def money_inr(text: Optional[str]) -> Optional[float]:
    """"6.5 Lakh (s)" -> 650000.0 ; "15094560" -> 15094560.0 ; "Nil" -> 0.0.

    ``Nil`` is a stated fact -- the buyer is saying there is no EMD -- so it
    becomes 0, which DRPL's card renders as "Nil". Absent stays None.
    """
    if not text:
        return None
    t = text.strip()
    if re.fullmatch(r"(nil|none|n\.?\s*/?\s*a\.?|not\s+applicable|--?)\.?", t, re.I):
        return 0.0
    m = _NUMBER.search(t)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    for rx, mult in _MULTIPLIERS:
        if rx.search(t):
            value *= mult
            break
    return value


def percent(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = _NUMBER.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    # A percentage above 100 is a misread, not a real term.
    return v if 0 <= v <= 100 else None


def integer(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d[\d,]*)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


_YES = re.compile(r"^\s*(yes|true|required|applicable|y)\b", re.I)
_NO = re.compile(r"^\s*(no|false|not\s+required|not\s+applicable|nil|n)\b", re.I)


def yes_no(text: Optional[str]) -> Optional[bool]:
    """"Yes | Complete" -> True. Anything unrecognised stays None."""
    if not text:
        return None
    t = text.strip()
    if _YES.match(t):
        return True
    if _NO.match(t):
        return False
    return None


_DATE_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
                 "%d/%m/%Y %H:%M:%S", "%d/%m/%Y")
_DATE_RE = re.compile(r"\d{2}[-/]\d{2}[-/]\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?")


def as_datetime(text: Optional[str]) -> Optional[str]:
    """GeM prints IST wall-clock. Returned as an ISO-8601 UTC instant.

    GeM states no offset anywhere in the document, and every date on the portal
    is Indian Standard Time -- so converting here is the difference between a
    closing date that is right and one that is five and a half hours early in
    every downstream view.
    """
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    raw = m.group(0)
    for fmt in _DATE_FORMATS:
        try:
            naive = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        # IST is UTC+5:30, no DST, ever.
        utc = naive.replace(tzinfo=timezone.utc).timestamp() - (5 * 3600 + 1800)
        return datetime.fromtimestamp(utc, tz=timezone.utc).isoformat()
    return None


def csv_list(text: Optional[str]) -> list[str]:
    """"Experience Criteria,Bidder Turnover,Certificate (Requested in ATC)"."""
    if not text:
        return []
    # GeM's own footnote follows the list in the same cell; it starts with "*".
    head = text.split("*", 1)[0]
    parts = [p.strip(" .;") for p in head.split(",")]
    return [p for p in parts if p and len(p) > 1]


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def emails(text: Optional[str]) -> list[str]:
    if not text:
        return []
    seen, out = set(), []
    for e in _EMAIL.findall(text):
        low = e.lower()
        if low not in seen:
            seen.add(low)
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# The parsed document
# ---------------------------------------------------------------------------


@dataclass
class Consignee:
    """One row of the "Consignees/Reporting Officer and Quantity" table."""

    serial: Optional[int] = None
    name: str = ""
    address: str = ""
    quantity: Optional[int] = None
    delivery_days: Optional[int] = None

    def as_line(self) -> str:
        bits = [b for b in (self.name, self.address) if b]
        line = " - ".join(bits)
        if self.quantity is not None:
            line += f" (qty {self.quantity}"
            if self.delivery_days is not None:
                line += f", {self.delivery_days} days"
            line += ")"
        elif self.delivery_days is not None:
            line += f" ({self.delivery_days} days)"
        return line


@dataclass
class GemBidDocument:
    """Everything a GeM bid document states. ``None`` means "not stated"."""

    # -- provenance --------------------------------------------------------
    bid_number: Optional[str] = None
    parent_bid_number: Optional[str] = None
    document_kind: str = "bid"            # bid | ra
    document_dated: Optional[str] = None
    page_count: int = 0

    # -- schedule ----------------------------------------------------------
    bid_end_date: Optional[str] = None
    bid_opening_date: Optional[str] = None
    bid_offer_validity_days: Optional[int] = None
    #: Service bids: "2 Year(s) 1 Day(s)". Goods bids do not print it.
    contract_period: Optional[str] = None

    # -- buyer -------------------------------------------------------------
    ministry: Optional[str] = None
    department: Optional[str] = None
    organisation: Optional[str] = None
    office: Optional[str] = None
    buyer_emails: list[str] = field(default_factory=list)
    grievance_contact: Optional[str] = None

    # -- subject -----------------------------------------------------------
    item_category: Optional[str] = None
    total_quantity: Optional[int] = None
    bis_required: Optional[bool] = None

    # -- money -------------------------------------------------------------
    estimated_value: Optional[float] = None
    emd_required: Optional[bool] = None
    emd_amount: Optional[float] = None
    emd_advisory_bank: Optional[str] = None
    epbg_required: Optional[bool] = None
    epbg_percentage: Optional[float] = None
    epbg_duration_months: Optional[int] = None
    epbg_advisory_bank: Optional[str] = None

    # -- ELIGIBILITY -------------------------------------------------------
    mse_relaxation: Optional[str] = None
    startup_relaxation: Optional[str] = None
    documents_required_from_seller: list[str] = field(default_factory=list)
    years_of_past_experience: Optional[str] = None
    min_average_annual_turnover: Optional[float] = None
    min_average_annual_turnover_text: Optional[str] = None
    oem_average_turnover: Optional[float] = None
    oem_average_turnover_text: Optional[str] = None
    turnover_eligibility: Optional[str] = None
    past_experience_similar: Optional[str] = None
    past_performance: Optional[str] = None
    additional_qualification: Optional[str] = None
    mse_purchase_preference: Optional[bool] = None
    mse_price_band_percent: Optional[float] = None
    mse_quantity_percent: Optional[float] = None
    mii_purchase_preference: Optional[bool] = None
    mii_price_band_percent: Optional[float] = None
    mii_local_supplier_class: Optional[str] = None

    pre_bid_date: Optional[str] = None
    pre_bid_venue: Optional[str] = None

    # -- process -----------------------------------------------------------
    type_of_bid: Optional[str] = None
    bid_to_ra_enabled: Optional[bool] = None
    evaluation_method: Optional[str] = None
    arbitration_clause: Optional[bool] = None
    mediation_clause: Optional[bool] = None
    bid_splitting: Optional[str] = None
    inspection_required: Optional[bool] = None
    inspection_type: Optional[str] = None
    inspection_agency: Optional[str] = None
    payment_timelines: Optional[str] = None
    technical_clarification_time: Optional[str] = None

    # -- delivery ----------------------------------------------------------
    consignees: list[Consignee] = field(default_factory=list)

    # -- attachments -------------------------------------------------------
    #: Files the document links to: the scope of work, the BOQ, the price
    #: breakup, the SLA. GeM prints "Click here to view the file" and puts the
    #: address only in the link annotation, so these are invisible to text
    #: extraction and are the documents an analyst most wants.
    attachments: list[Attachment] = field(default_factory=list)

    # -- free text ---------------------------------------------------------
    technical_specifications: Optional[str] = None
    buyer_added_terms: Optional[str] = None
    buyer_added_atc: Optional[str] = None
    scope_of_supply: Optional[str] = None

    # -- diagnostics -------------------------------------------------------
    #: Catalogue keys that matched. The drift alarm reads this.
    fields_found: list[str] = field(default_factory=list)
    #: True when the document parsed but yielded so little that a human (or a
    #: model) should look at it. Never silently swallowed.
    needs_ai_fallback: bool = False
    parse_notes: list[str] = field(default_factory=list)

    # -- derived -----------------------------------------------------------

    @property
    def delivery_location(self) -> Optional[str]:
        """The consignee addresses, as one line for DRPL's card."""
        parts = [c.address or c.name for c in self.consignees if (c.address or c.name)]
        if not parts:
            return None
        uniq: list[str] = []
        for p in parts:
            if p not in uniq:
                uniq.append(p)
        return " ; ".join(uniq)[:500]

    @property
    def delivery_timeline(self) -> Optional[str]:
        """Delivery days for goods; the contract period for services.

        A service bid has no delivery days -- its consignee table names a
        reporting officer, not a delivery -- and states the engagement in the
        header as "Contract Period: 2 Year(s) 1 Day(s)" instead. On the live
        ministry sweep that was 712 of 1,782 bids, every one shipping with
        the timeline blank until this fallback.
        """
        days = [c.delivery_days for c in self.consignees if c.delivery_days is not None]
        if days:
            return f"{min(days)} days" if min(days) == max(days) else f"{min(days)}-{max(days)} days"
        if self.contract_period:
            return f"contract period {self.contract_period}"
        return None

    def eligibility_text(self) -> Optional[str]:
        """The eligibility block, rendered for ``Tender.eligibility_criteria``.

        Written as stated label/value lines rather than prose: an analyst
        reading this needs to see exactly what GeM published, and a paraphrase
        of a qualifying condition is a liability. Only stated facts appear --
        a field the document omits contributes no line at all, so the absence
        of a turnover requirement is visible as an absence.
        """
        rows: list[tuple[str, str]] = []

        def add(label: str, value: Any) -> None:
            if value is None or value == "" or value == []:
                return
            if isinstance(value, bool):
                value = "Yes" if value else "No"
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            rows.append((label, str(value).strip()))

        add("Minimum average annual turnover", self.min_average_annual_turnover_text)
        add("OEM average turnover", self.oem_average_turnover_text)
        add("Years of past experience required", self.years_of_past_experience)
        add("Past experience of similar services required", self.past_experience_similar)
        add("Pre-bid meeting", self.pre_bid_date)
        add("Pre-bid venue", self.pre_bid_venue)
        add("Turnover eligibility", self.turnover_eligibility)
        add("Past performance", self.past_performance)
        add("Documents required from seller", self.documents_required_from_seller)
        add("MSE relaxation (experience & turnover)", self.mse_relaxation)
        add("Startup relaxation (experience & turnover)", self.startup_relaxation)
        add("MSE purchase preference", self.mse_purchase_preference)
        add("MSE price band (L1 + x%)", self.mse_price_band_percent)
        add("MSE share of bid quantity (%)", self.mse_quantity_percent)
        add("MII purchase preference", self.mii_purchase_preference)
        add("MII price band (L1 + x%)", self.mii_price_band_percent)
        add("Local supplier class restriction", self.mii_local_supplier_class)
        add("EMD required", self.emd_required)
        add("EMD amount (INR)", self.emd_amount)
        add("ePBG required", self.epbg_required)
        add("ePBG percentage", self.epbg_percentage)
        add("ePBG duration (months)", self.epbg_duration_months)
        add("Inspection required", self.inspection_required)
        add("Inspection type", self.inspection_type)
        add("Inspection agency", self.inspection_agency)
        add("Additional qualification / data required", self.additional_qualification)
        add("BIS required", self.bis_required)
        add("Evaluation method", self.evaluation_method)
        add("Bid type", self.type_of_bid)
        add("Arbitration clause", self.arbitration_clause)
        add("Mediation clause", self.mediation_clause)

        if not rows:
            return None
        width = max(len(r[0]) for r in rows)
        return "\n".join(f"{k.ljust(width)} : {v}" for k, v in rows)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["delivery_location"] = self.delivery_location
        d["delivery_timeline"] = self.delivery_timeline
        return d


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_BID_NO = re.compile(r"GEM/\d{4}/[BR]/\d+", re.I)
_PARENT_HINT = re.compile(
    r"refer\s+to\s+Bid\s+document\s+of\s+(GEM/\d{4}/B/\d+)", re.I)
_DATED = re.compile(r"Dated\s*:\s*(\d{2}-\d{2}-\d{4})", re.I)

#: Below this many recognised fields, a "successful" parse is not one. Chosen
#: from the sample: the thinnest real reverse-auction document yielded 9.
_MIN_FIELDS_BID = 12
_MIN_FIELDS_RA = 6


def _line_spans(lines: list[str]) -> list[tuple[int, int]]:
    """Offset of each line within ``" ".join(lines)``."""
    spans, pos = [], 0
    for ln in lines:
        spans.append((pos, pos + len(ln)))
        pos += len(ln) + 1  # the joining space
    return spans


def _line_index(spans: list[tuple[int, int]], offset: int) -> int:
    """Which line an offset falls in. Binary search; spans are sorted."""
    lo, hi = 0, len(spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        s, e = spans[mid]
        if offset < s:
            hi = mid - 1
        elif offset > e:
            lo = mid + 1
        else:
            return mid
    return max(0, min(len(spans) - 1, lo))


def _assign_values(doc: NormalizedDocument, found: list[Found]) -> None:
    """Fill ``Found.value`` for each label, working in whole lines.

    This is the one decision that makes the parser clean. Values could be cut
    straight out of the flat string -- ``flat[end:next.start]`` -- but the next
    label's match begins at its slash, so everything the mojibake left in front
    of that slash would land at the tail of the previous value: "Mumbai %",
    "Ministry Of Railways A>". Stripping that after the fact needs a heuristic
    for "is this token junk", and there is no honest one, because ``No`` and
    ``T`` are the same shape.

    Taking whole *lines* between one label's last line and the next label's
    first line sidesteps it entirely: the junk always sits on the next label's
    own line, and that line is excluded by construction. Text following a label
    on its own line is kept, which is what makes one-line statements work.
    """
    spans = _line_spans(doc.lines)
    n = len(doc.lines)

    for i, f in enumerate(found):
        label_end_line = _line_index(spans, max(f.start, f.end - 1))

        # The next boundary is the next label that starts *after this one ends*
        # -- not simply the next entry in the list. GeM prints several headings
        # twice in a row ("MII Purchase Preference" as the section rule and
        # again as the field under it), so two matches routinely share a span.
        # Taking the list neighbour makes each of them bound the other at zero
        # width, and every such field reads as unstated.
        nxt = None
        for cand in found[i + 1:]:
            if cand.start >= f.end:
                nxt = cand
                break
        stop_line = _line_index(spans, nxt.start) if nxt else n

        pieces: list[str] = []
        # Remainder of the label's own line, if the value was printed inline.
        line_s, line_e = spans[label_end_line]
        if f.end < line_e:
            tail = doc.lines[label_end_line][f.end - line_s:].strip()
            if tail and not is_junk_line(tail):
                pieces.append(tail)
        # Whole lines up to (not including) the line the next label starts on.
        # A wrapped Hindi label can occupy a line of its own just before the
        # next label's line; those are dropped rather than joined in.
        for li in range(label_end_line + 1, min(stop_line, n)):
            line = doc.lines[li]
            if is_junk_line(line):
                continue
            pieces.append(line)

        f.value = _clean_value(" ".join(pieces))


_VALUE_LEAD = re.compile(r"^[\s:;,\-/]+")

#: Where a value stops even if no next label was found. These are the standing
#: paragraphs GeM appends to every document; without them the last field on a
#: page swallows the whole disclaimer, which is how a reverse auction's
#: "Startup Exemption: No" came back as six hundred words of GTC clause 26.
_BOILERPLATE_STOP = re.compile(
    r"(Participation\s+by\s+the\s+bidder\s+in\s+RA\s+process"
    r"|Please\s+refer\s+to\s+Bid\s+document\s+of"
    r"|This\s+Bid\s+is\s+also\s+governed\s+by"
    r"|In\s+terms\s+of\s+GeM\s+GTC\s+clause"
    r"|\*In\s+case\s+any\s+bidder\s+is\s+seeking\s+exemption"
    r"|The\s+Additional\s+Terms\s+and\s+Conditions\s+\(ATC\)\s+have\s+been)",
    re.I,
)

#: No single stated value on a GeM bid document is longer than this. A capture
#: that runs past it has certainly swallowed the next block, and truncating is
#: the honest failure: a clipped value is visibly clipped, while a value that
#: quietly contains the following section is not.
_MAX_VALUE_CHARS = 1200


def _clean_value(text: str) -> str:
    v = _VALUE_LEAD.sub("", text or "").strip()
    v = re.sub(r"\s+", " ", v)
    stop = _BOILERPLATE_STOP.search(v)
    if stop and stop.start() > 0:
        v = v[: stop.start()].strip()
    if len(v) > _MAX_VALUE_CHARS:
        v = v[:_MAX_VALUE_CHARS].rsplit(" ", 1)[0] + " ..."
    return v.strip(" .,;:")


#: The column headings of the consignee table. GeM varies them by bid type --
#: a goods bid has Quantity and Delivery Days, a services bid has "Additional
#: Requirement" or "Number of Vehicles Required" instead -- so they are matched
#: as a set of known headings and dropped, rather than relied on positionally.
_CONSIGNEE_HEADINGS = re.compile(
    r"^(?:S\.?\s*No\.?|Consignee|Reporting\s*/?\s*Officer|Consignee\s*"
    r"Reporting/?Officer|Address|Quantity|Delivery\s+Days|Additional\s+"
    r"Requirement|Number\s+of\s+Vehicles\s+Required|Item|Unit)$",
    re.I,
)

#: A delivery-day count is small. A six-digit number on an address line is a
#: PIN code, and reading it as a delivery period -- which an earlier version of
#: this function did, reporting "811214 days" -- is worse than reporting
#: nothing, because it reaches the tender card as a plausible-looking number.
_MAX_DELIVERY_DAYS = 3650


def _parse_consignees(lines: list[str]) -> list[Consignee]:
    """Read the "Consignees/Reporting Officer and Quantity" table.

    The table extracts as: serial, name, address, quantity, delivery-days --
    but PDF extraction splits a row across lines unpredictably, so this reads
    the block between the section heading and the next heading and pulls the
    numbers off the end of each logical row. A row it cannot read fully still
    contributes its address, because a delivery location with no day count is
    far more useful than nothing.
    """
    out: list[Consignee] = []
    start = None
    for i, ln in enumerate(lines):
        if re.search(r"Consignees?\s*/?\s*Reporting\s*/?\s*Officer", ln, re.I):
            start = i + 1
            break
    if start is None:
        return out

    stop = len(lines)
    for i in range(start, len(lines)):
        # "Special terms and conditions" is a separate block that follows the
        # consignee table on some bids; without it here, its whole text was
        # joining the delivery address.
        if re.search(r"Buyer\s+Added\s+Bid\s+Specific|Disclaimer|Thank\s+You"
                     r"|This\s+Bid\s+is\s+also\s+governed"
                     r"|Special\s+terms\s+and\s+conditions"
                     r"|Additional\s+Details|Scope\s+of\s+Supply",
                     lines[i], re.I):
            stop = i
            break

    # Everything from the section heading to the first serial number is the
    # column-heading row. Cutting there rather than matching heading names is
    # what makes this survive GeM's per-bid-type headings: a services bid has
    # "Additional Requirement" or "Number of Vehicles Required" where a goods
    # bid has "Quantity" and "Delivery Days", and the headings wrap, so "Days"
    # arrives on a line of its own and no name-matching list catches it.
    cells: list[str] = []
    for raw in lines[start:stop]:
        cell = raw.strip()
        # The heading cells still carry their Hindi remnant and slash.
        cell = re.sub(r"^[^A-Za-z0-9]*/\s*", "", cell).strip()
        if not cell or is_junk_line(cell):
            continue
        cells.append(cell)

    first_serial = None
    for i, cell in enumerate(cells):
        if re.fullmatch(r"\d{1,3}", cell):
            first_serial = i
            break
    if first_serial is None:
        return out
    body = cells[first_serial:]
    if not body:
        return out

    # Rows are separated by the serial number that opens each one. Most bids
    # have exactly one consignee, in which case this yields a single row --
    # which is the correct answer, not a degenerate one.
    rows: list[list[str]] = []
    buf: list[str] = []
    for i, cell in enumerate(body):
        nxt = body[i + 1] if i + 1 < len(body) else ""
        # A row opens with its serial number -- but so does the quantity
        # column look like one, and splitting on "any bare number" put the
        # quantity and the delivery days into a row of their own, which is
        # why a single-consignee bid reported no delivery period at all.
        # What separates them is what comes next: a serial is followed by the
        # consignee's name, a quantity by the delivery-day count.
        # The final cell can never open a row -- it is the last row's delivery
        # day count, and treating it as a serial stranded it in a row of its
        # own and reported "1 day" where the document said 30.
        starts_row = (
            buf
            and nxt
            and re.fullmatch(r"\d{1,3}", cell)
            and not re.fullmatch(r"\d+", nxt)
        )
        if starts_row:
            rows.append(buf)
            buf = [cell]
        else:
            buf.append(cell)
    if buf:
        rows.append(buf)

    for row in rows[:25]:
        c = Consignee()
        cells = list(row)
        if cells and re.fullmatch(r"\d{1,3}", cells[0]):
            c.serial = int(cells.pop(0))

        # Quantity and delivery days are the trailing numeric cells. Taken from
        # the end and bounds-checked, so an address that ends in a PIN code
        # cannot supply them.
        trailing: list[int] = []
        while cells and re.fullmatch(r"\d{1,7}", cells[-1]) and len(trailing) < 2:
            trailing.insert(0, int(cells.pop()))
        if len(trailing) == 2:
            c.quantity = trailing[0]
            if trailing[1] <= _MAX_DELIVERY_DAYS:
                c.delivery_days = trailing[1]
        elif len(trailing) == 1:
            # One number: a services bid states days only, a goods bid quantity
            # only. Days are small; a quantity can be anything.
            if trailing[0] <= _MAX_DELIVERY_DAYS:
                c.delivery_days = trailing[0]
            else:
                c.quantity = trailing[0]

        if cells:
            # The first cell is the officer's name when it reads like one.
            if re.fullmatch(r"[A-Za-z][A-Za-z.\-'\s]{2,60}", cells[0]):
                c.name = cells.pop(0).strip()
            c.address = ", ".join(x.strip(" ,") for x in cells if x.strip(" ,"))[:400]

        if c.name or c.address:
            out.append(c)
    return out


#: A heading line is short and is *only* the heading. Requiring that is the
#: same rule that keeps ``Estimated Bid Value`` out of the boilerplate
#: paragraph, applied to the narrative blocks: "Technical Specifications"
#: appears mid-sentence in the MSE relaxation clause on most documents
#: ("...subject to meeting of quality and technical specifications"), and
#: matching it there made the specifications field open with a paragraph of
#: policy text on every bid that mentions MSE.
_HEADING_MAX_CHARS = 90


def _is_heading(line: str, heading_rx: str) -> bool:
    """Is this line the heading itself, rather than prose that mentions it?

    Two conditions, and both are needed. The heading must sit at the start of
    the line or immediately after the slash that separated it from its Hindi
    twin -- the live line is ``S /Technical Specifications``, so neither a
    plain ``startswith`` nor a bare ``search`` is right. And the line must be
    short: the same words appear mid-sentence in the MSE relaxation clause of
    most documents, and that sentence runs well past any heading's length.
    """
    core = line.strip()
    if len(core) > _HEADING_MAX_CHARS:
        return False
    return re.search(
        r"(?:^|/)\s*(?:\d+\.\s*)?(?:" + heading_rx + r")", core, re.I
    ) is not None


def _section_text(lines: list[str], start_rx: str, stop_rx: str,
                  limit: int = 8000) -> Optional[str]:
    """The text of one narrative block, by heading.

    Junk lines are dropped here as well as in value capture, because a
    specification table is mostly bilingual headers and leaving their mojibake
    in produces exactly the soup DRPL's card used to show: "* S / As per GeM
    Category Specification /Specification S /Specification Name B Q /Bid
    Requirement". What remains after dropping them is the readable half.
    """
    start = None
    for i, ln in enumerate(lines):
        if _is_heading(ln, start_rx):
            start = i + 1
            break
    if start is None:
        return None
    stop = len(lines)
    for i in range(start, len(lines)):
        if re.search(stop_rx, lines[i], re.I):
            stop = i
            break
    kept = []
    for line in lines[start:stop]:
        if not line.strip() or is_junk_line(line):
            continue
        # A heading cell still carries its Hindi remnant and the slash that
        # separated it. Keep the English, drop the debris in front of it.
        kept.append(re.sub(r"^[^A-Za-z0-9]*/\s*", "", line).strip())
    body = re.sub(r"\s+", " ", " ".join(k for k in kept if k)).strip()
    return body[:limit] or None


def parse_text(text: str, *, bid_number: Optional[str] = None,
               links: Optional[list[str]] = None) -> GemBidDocument:
    """Parse already-extracted PDF text. The seam the tests drive."""
    pages = page_count_of(text)
    doc = NormalizedDocument.from_text(text)
    return _parse(doc, pages=pages, bid_number=bid_number, raw_text=text,
                  links=links or [])


def parse_bid_document(data: bytes, *, bid_number: Optional[str] = None) -> GemBidDocument:
    """Parse GeM bid-document PDF bytes.

    Raises ``ValueError`` if the bytes are not a readable PDF -- GeM answers a
    document URL with an HTML error page and a 200 often enough that the caller
    must handle it, and a silent empty result would hide it.
    """
    from collector.gemdoc.normalize import extract_text

    text = extract_text(data)
    return parse_text(text, bid_number=bid_number, links=extract_links(data))


def _parse(doc: NormalizedDocument, *, pages: int, bid_number: Optional[str],
           raw_text: str, links: Optional[list[str]] = None) -> GemBidDocument:
    out = GemBidDocument(page_count=pages)
    if doc.is_empty:
        out.needs_ai_fallback = True
        out.parse_notes.append("document extracted to no text (scanned image?)")
        return out

    spans = _line_spans(doc.lines)
    found = find_labels(
        doc.flat,
        line_starts={s for s, _ in spans},
        line_ends={e for _, e in spans},
    )
    _assign_values(doc, found)
    # The first occurrence that actually carries a value wins -- not simply the
    # first occurrence. GeM prints "MII Purchase Preference" as a section
    # heading and then immediately again as the field inside it, so the first
    # match is a heading with an empty span beneath it. Taking it would report
    # the preference as unstated on every document that has one.
    values: dict[str, str] = {}
    for f in found:
        if f.label.kind is Kind.SECTION:
            continue
        if f.value and not values.get(f.label.key):
            values[f.label.key] = f.value
        else:
            values.setdefault(f.label.key, f.value)

    out.fields_found = sorted(values)
    g = values.get

    # -- provenance --------------------------------------------------------
    flat = doc.flat
    out.document_kind = "ra" if re.search(r"\bRA\s+Number\s*:", raw_text) or \
        re.search(r"\bRA\s+Details\b", flat) else "bid"
    nums = _BID_NO.findall(raw_text)
    out.bid_number = bid_number or (nums[0].upper() if nums else None)
    parent = _PARENT_HINT.search(flat)
    if parent:
        out.parent_bid_number = parent.group(1).upper()
    elif out.document_kind == "ra":
        # An RA document names the parent bid somewhere even when the sentence
        # is worded differently; take the first B-number that is not itself.
        for n in nums:
            if "/B/" in n.upper() and n.upper() != (out.bid_number or ""):
                out.parent_bid_number = n.upper()
                break
    d = _DATED.search(raw_text)
    if d:
        out.document_dated = as_datetime(d.group(1))

    # -- schedule ----------------------------------------------------------
    out.bid_end_date = as_datetime(g("bid_end_date") or g("ra_end_date"))
    out.bid_opening_date = as_datetime(g("bid_opening_date") or g("ra_start_date"))
    out.bid_offer_validity_days = integer(g("bid_offer_validity"))
    out.contract_period = g("contract_period") or None

    # -- buyer -------------------------------------------------------------
    out.ministry = g("ministry") or None
    out.department = g("department") or None
    out.organisation = g("organisation") or None
    out.office = g("office") or None
    out.grievance_contact = g("grievance_contact") or None
    out.buyer_emails = emails(g("grievance_contact"))
    out.attachments, mailto = classify_links(links or [])
    for addr in mailto:
        if addr not in out.buyer_emails:
            out.buyer_emails.append(addr)

    # -- subject -----------------------------------------------------------
    out.item_category = g("item_category") or None
    out.total_quantity = integer(g("total_quantity"))
    out.bis_required = yes_no(g("bis_required"))

    # -- money -------------------------------------------------------------
    out.estimated_value = money_inr(g("estimated_bid_value"))
    out.emd_required = yes_no(g("emd_required"))
    out.emd_amount = money_inr(g("emd_amount"))
    out.emd_advisory_bank = g("emd_advisory_bank") or None
    if out.emd_required is False and out.emd_amount is None:
        # "Required: No" is a statement that there is no EMD, not a gap.
        out.emd_amount = 0.0
    elif out.emd_required is None and out.emd_amount:
        # The other half of the same convention: a bid that *has* an EMD
        # prints the advisory bank and the amount and never prints a
        # "Required" line at all. Leaving the flag unset here would show an
        # EMD of Rs 2,55,840 next to "EMD required: unknown".
        out.emd_required = True

    out.epbg_required = yes_no(g("epbg_required"))
    out.epbg_percentage = percent(g("epbg_percentage"))
    out.epbg_duration_months = integer(g("epbg_duration"))
    out.epbg_advisory_bank = g("epbg_advisory_bank") or None
    if out.epbg_required is None and (out.epbg_percentage or out.epbg_advisory_bank):
        out.epbg_required = True

    # -- eligibility -------------------------------------------------------
    out.mse_relaxation = g("mse_relaxation") or None
    out.startup_relaxation = g("startup_relaxation") or None
    out.documents_required_from_seller = csv_list(g("documents_required_from_seller"))
    out.years_of_past_experience = g("years_of_past_experience") or None
    turnover_text = g("min_average_annual_turnover") or g("bidder_turnover") or None
    out.min_average_annual_turnover_text = turnover_text
    out.min_average_annual_turnover = money_inr(turnover_text)
    out.oem_average_turnover_text = g("oem_average_turnover") or None
    out.oem_average_turnover = money_inr(g("oem_average_turnover"))
    out.turnover_eligibility = g("turnover_eligibility") or None
    out.past_experience_similar = g("past_experience_similar") or None
    out.past_performance = g("past_performance") or None
    out.additional_qualification = g("additional_qualification") or None
    out.mse_purchase_preference = yes_no(g("mse_purchase_preference"))
    out.mse_price_band_percent = percent(g("mse_price_band"))
    out.mse_quantity_percent = percent(g("mse_max_percentage"))
    out.mii_purchase_preference = yes_no(g("mii_purchase_preference"))
    out.mii_price_band_percent = percent(g("mii_price_band"))
    out.mii_local_supplier_class = g("mii_local_supplier_class") or None

    # -- process -----------------------------------------------------------
    out.type_of_bid = g("type_of_bid") or None
    out.bid_to_ra_enabled = yes_no(g("bid_to_ra_enabled"))
    out.evaluation_method = g("evaluation_method") or None
    out.arbitration_clause = yes_no(g("arbitration_clause"))
    out.mediation_clause = yes_no(g("mediation_clause"))
    out.bid_splitting = g("bid_splitting") or None
    out.inspection_required = yes_no(g("inspection_required"))
    out.inspection_type = g("inspection_type") or None
    out.inspection_agency = g("inspection_agency") or None
    out.payment_timelines = g("payment_timelines") or None
    out.pre_bid_date = as_datetime(g("pre_bid_date"))
    out.pre_bid_venue = g("pre_bid_venue") or None
    out.technical_clarification_time = g("technical_clarification_time") or None

    # -- delivery and free text -------------------------------------------
    out.consignees = _parse_consignees(doc.lines)
    out.technical_specifications = _section_text(
        doc.lines, r"Technical\s+Specifications",
        r"Consignees?/\s*Reporting|Buyer\s+Added\s+Bid\s+Specific")
    out.buyer_added_terms = _section_text(
        doc.lines, r"Buyer\s+Added\s+Bid\s+Specific\s+Terms\s+and\s+Conditions",
        r"Disclaimer|Thank\s+You|This\s+Bid\s+is\s+also\s+governed")
    out.buyer_added_atc = _section_text(
        doc.lines, r"Buyer\s+Added\s+text\s+based\s+ATC\s+clauses",
        r"\d+\.\s*Certificates|Disclaimer|Thank\s+You")
    out.scope_of_supply = _section_text(
        doc.lines, r"\d*\.?\s*Scope\s+of\s+Supply",
        r"\d+\.\s*(Buyer\s+Added|Certificates)|Disclaimer")

    # -- diagnostics -------------------------------------------------------
    threshold = _MIN_FIELDS_RA if out.document_kind == "ra" else _MIN_FIELDS_BID
    if len(values) < threshold:
        out.needs_ai_fallback = True
        out.parse_notes.append(
            f"only {len(values)} of the expected fields matched "
            f"(threshold {threshold} for a {out.document_kind} document)"
        )
    return out
