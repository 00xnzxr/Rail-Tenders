"""
DRPL Collector - the GeM bid-document label catalogue.

This is the only file that knows what GeM calls things. Everything else in
``gemdoc`` is mechanism. When GeM changes its template, the change lands here
and nowhere else.

THE EXTRACTION MODEL
--------------------
After ``normalize.py`` has dropped the unrecoverable Devanagari, every field in
the document reads as::

    <junk>/English Label Text   <value>   <junk>/Next English Label   <value> ...

all on one flat line. So a field's value is simply *the text between the end of
its label and the start of the next label*. That is the whole algorithm, and it
is why this file is a list of labels rather than a list of value patterns: we
never have to describe what a value looks like, which is the part that varies.

Two things make it reliable rather than merely plausible:

**Position, not just text.** A real label occupies whole lines: it starts a
line (or follows the slash that separated it from its Hindi twin) and ends one,
with its value on the lines below. Prose containing the same words does
neither. This matters more than it sounds: every document that *has* an
estimated value also carries the sentence "Estimated Bid Value indicated above
is being declared solely for the purpose of guidance..." in its closing
boilerplate. Matching on text alone captures that paragraph of legalese as the
bid's worth. Requiring the match to end a line rejects it, without this file
needing to know anything about the prose. ``_is_anchored`` is the whole rule.

**Longest-match-wins overlap resolution.** ``EMD Detail`` and ``Detail`` would
both match the same span; ``MSE Purchase Preference`` contains ``Purchase
Preference``. Resolving overlaps by preferring the longer match means a
catalogue entry can be added without auditing every other entry for containment.

SECTION SCOPING
---------------
Three labels are ambiguous on their own because GeM reuses them under different
headings::

    /EMD Detail      /Required  No
    /ePBG Detail     /Required  No   /Percentage  3   /Duration of ePBG ...

``Required`` means different things in those two places. Rather than inventing
brittle composite patterns, such labels declare ``section=`` and are bound at
parse time to the nearest *preceding* section anchor. A ``Required`` with no
governing section is discarded rather than guessed at -- an EMD wrongly read as
"not required" is worse than an EMD read as "unknown", because only one of
those two prompts somebody to open the PDF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

__all__ = ["Kind", "Label", "CATALOGUE", "SECTIONS", "find_labels", "Found"]


class Kind(str, Enum):
    """What a catalogue entry is for."""

    #: An ordinary label whose value is the text that follows it.
    FIELD = "field"
    #: A heading. Carries no value of its own but scopes the fields under it,
    #: and bounds the value of whatever field precedes it.
    SECTION = "section"


def _pattern(text: str) -> str:
    """Literal label text -> a regex tolerant of the wrapping GeM applies.

    Whitespace becomes ``\\s+`` because a label broken across PDF lines is
    re-joined with a single space, but the original may have had none, one, or
    several. Everything else is escaped: label text contains ``/``, ``(``,
    ``%`` and ``.``, none of which we want interpreted.
    """
    return re.escape(text).replace(r"\ ", r"\s+")


@dataclass(frozen=True)
class Label:
    """One entry in the catalogue."""

    #: The key this field lands on in the parsed result.
    key: str
    #: Literal label texts, most specific first. Several because GeM words the
    #: same field differently on a bid ("MSE Relaxation for...") and on a
    #: reverse auction ("MSE Exemption for..."), and both are live today.
    texts: tuple[str, ...]
    kind: Kind = Kind.FIELD
    #: Require the slash that separated the Hindi label from the English one.
    #: Default True; see the module docstring for why this matters so much.
    anchored: bool = True
    #: Bind this field to the nearest preceding section with this key. A match
    #: whose governing section differs is discarded.
    section: Optional[str] = None

    @property
    def regexes(self) -> tuple[re.Pattern, ...]:
        """The label text alone. Anchoring is checked positionally instead.

        Baking a ``/`` into the pattern was the first design, and it broke on
        reverse-auction documents: their Hindi is set in a font that extracts
        to nothing at all rather than to junk, so an RA prints a bare
        ``Ministry/State Name`` with no slash in front of it while a bid prints
        ``/!  /Ministry/State Name``. Both are real labels. ``find_labels``
        accepts either shape -- see ``_is_anchored``.
        """
        return tuple(re.compile(_pattern(t), re.IGNORECASE) for t in self.texts)


def _f(key: str, *texts: str, **kw) -> Label:
    return Label(key=key, texts=texts, **kw)


def _s(key: str, *texts: str, **kw) -> Label:
    return Label(key=key, texts=texts, kind=Kind.SECTION, **kw)


# ---------------------------------------------------------------------------
# Sections. Order here does not matter -- position in the document does.
# ---------------------------------------------------------------------------

SECTIONS: tuple[Label, ...] = (
    _s("sec_bid_details", "Bid Details"),
    _s("sec_ra_details", "RA Details", anchored=False),
    _s("sec_emd", "EMD Detail"),
    _s("sec_epbg", "ePBG Detail"),
    _s("sec_mii", "MII Purchase Preference"),
    _s("sec_mse", "MSE Purchase Preference"),
    _s("sec_treds", "TReDS Payment Details"),
    _s("sec_tech_specs", "Technical Specifications"),
    _s("sec_consignees", "Consignees/Reporting Officer and Quantity",
       "Consignees/Reporting Officer"),
    _s("sec_buyer_terms", "Buyer Added Bid Specific Terms and Conditions"),
    _s("sec_buyer_atc", "Buyer Added Bid Specific ATC"),
    _s("sec_prebid", "Pre-Bid Detail(s)", "Pre-Bid Details", "Pre-Bid Detail"),
    _s("sec_disclaimer", "Disclaimer", "Thank You"),
)


# ---------------------------------------------------------------------------
# Fields.
# ---------------------------------------------------------------------------

CATALOGUE: tuple[Label, ...] = SECTIONS + (
    # -- identity and schedule ------------------------------------------
    _f("bid_end_date", "Bid End Date/Time"),
    _f("bid_opening_date", "Bid Opening Date/Time"),
    _f("bid_offer_validity", "Bid Offer Validity (From End Date)"),
    # Service bids only. Goods state delivery days per consignee; a service
    # has no delivery and states the engagement here instead -- 40% of the
    # railway ministry on the live sweep, all of them with a blank timeline
    # until this label was read.
    _f("contract_period", "Contract Period"),
    _f("ra_start_date", "RA Start Date/Time"),
    _f("ra_end_date", "RA End Date/Time"),

    # -- buyer ------------------------------------------------------------
    _f("ministry", "Ministry/State Name"),
    _f("department", "Department Name"),
    _f("organisation", "Organisation Name"),
    _f("office", "Office Name"),
    _f("grievance_contact", "Contact details of Grievance redressal"),

    # -- what is being bought ---------------------------------------------
    _f("total_quantity", "Total Quantity"),
    _f("item_category", "Item Category"),
    _f("bis_required", "Bis Required", anchored=False),
    _f("gemarpts_search_strings", "Searched Strings used in GeMARPTS"),
    _f("gemarpts_result", "Searched Result generated in GeMARPTS",
       "Result generated in GeMARPTS"),
    _f("gemarpts_categories", "Relevant Categories selected for notification"),

    # -- THE ELIGIBILITY BLOCK --------------------------------------------
    # This is the module DRPL's tender card calls "eligibility criteria", and
    # every one of these is a stated fact on the page rather than an inference.
    # Four wordings for two fields. A bid says "Relaxation", a reverse
    # auction says "Exemption", and either may or may not name experience
    # alongside turnover. All four are live; the shorter forms must come last
    # so longest-match-wins picks the specific one when both apply.
    _f("mse_relaxation",
       "MSE Relaxation for Years Of Experience and Turnover",
       "MSE Exemption for Years of Experience and Turnover",
       "MSE Relaxation for Turnover", "MSE Exemption for Turnover"),
    _f("startup_relaxation",
       "Startup Relaxation for Years of Experience and Turnover",
       "Startup Exemption for Years of Experience and Turnover",
       "Startup Relaxation for Turnover", "Startup Exemption for Turnover"),
    _f("documents_required_from_seller", "Document required from seller"),
    _f("years_of_past_experience",
       "Years of Past Experience Required for same/similar service",
       "Years of Past Experience Required"),
    _f("turnover_eligibility", "Turnover eligibility criteria"),
    _f("past_experience_similar",
       "Past Experience of Similar Services required",
       "Past Experience of Similar Services"),
    _f("min_average_annual_turnover",
       "Minimum Average Annual Turnover of the bidder (For 3 Years)",
       "Minimum Average Annual Turnover of the bidder"),
    _f("oem_average_turnover",
       "OEM Average Turnover (Last 3 Years)", "OEM Average Turnover"),
    _f("past_performance", "Past Performance"),
    _f("additional_qualification", "Additional Qualification/Data Required"),
    _f("excel_upload_required", "Excel Upload Required"),

    # -- money -------------------------------------------------------------
    _f("estimated_bid_value",
       "Estimated Bid Value in INR (Inclusive of all taxes)",
       "Estimated Bid Value in INR", "Estimated Bid Value"),
    # When an EMD actually applies, GeM prints the advisory bank and the
    # amount and omits the "Required" line entirely; only a bid with no EMD
    # says "Required: No". Verified on live documents both ways -- so the
    # amount, not the flag, is the load-bearing field here, and the parser
    # infers the flag from it.
    _f("emd_required", "Required", section="sec_emd"),
    _f("emd_amount", "EMD Amount", "Amount", section="sec_emd"),
    _f("emd_percentage", "EMD Percentage", "Percentage", section="sec_emd"),
    _f("emd_advisory_bank", "Advisory Bank", section="sec_emd"),
    _f("epbg_required", "Required", section="sec_epbg"),
    _f("epbg_advisory_bank", "Advisory Bank", section="sec_epbg"),
    _f("epbg_percentage", "ePBG Percentage(%)", "ePBG Percentage", "Percentage",
       section="sec_epbg"),
    _f("epbg_duration", "Duration of ePBG required (Months)",
       "Duration of ePBG required", "Duration", section="sec_epbg"),

    # -- preferences and process ------------------------------------------
    _f("mii_purchase_preference", "MII Purchase Preference", section="sec_mii"),
    _f("mii_price_band", "Purchase Preference to MII sellers availabele upto price within L1+X%",
       "Purchase Preference to MII sellers available upto price within L1+X%",
       section="sec_mii"),
    _f("mii_max_percentage",
       "Maximum Percentage of Bid quantity for MII purchase preference",
       section="sec_mii"),
    _f("mii_local_supplier_class",
       "Allow participation only from Class 1/Class 2 local suppliers as per the "
       "Public procurement(Preference to Make-in-india) order 2017 date "
       "16.09.2020(as amended and applicable time to time)",
       "Allow participation only from Class 1/Class 2 local suppliers",
       section="sec_mii"),
    _f("mse_purchase_preference", "MSE Purchase Preference", section="sec_mse"),
    _f("mse_price_band",
       "Purchase Preference to MSE OEMs/ Service Provider available upto price within L1+X%",
       section="sec_mse"),
    _f("mse_max_percentage",
       "Percentage of Bid quantity/amount for MSE OEMs/ Service Provider Purchase preference",
       section="sec_mse"),
    _f("bid_splitting", "Bid splitting not applied.", "Bid splitting"),

    # -- terms -------------------------------------------------------------
    _f("bid_to_ra_enabled", "Bid to RA enabled"),
    _f("type_of_bid", "Type of Bid"),
    _f("technical_clarification_time",
       "Time allowed for Technical Clarifications during technical evaluation"),
    _f("inspection_required",
       "Inspection Required (By Empanelled Inspection Authority / Agencies "
       "pre-registered with GeM)",
       "Inspection Required"),
    _f("inspection_by_buyer_agency",
       "Inspection to be carried out by Buyers own empanelled agency",
       anchored=False),
    _f("inspection_type", "Type Of Inspection", anchored=False),
    _f("inspection_agency", "Name of the Empanelled Inspection Agency/ Authority",
       anchored=False),
    _f("auto_crac_days", "Auto CRAC Days", anchored=False),
    _f("evaluation_method", "Evaluation Method"),
    _f("price_breakup_required",
       "Financial Document Indicating Price Breakup Required"),
    _f("pre_bid_date", "Pre-Bid Date and Time", "Pre-Bid Date"),
    _f("pre_bid_venue", "Pre-Bid Venue"),
    _f("bidder_turnover", "Bidder Turnover"),
    _f("arbitration_clause", "Arbitration Clause"),
    _f("mediation_clause", "Mediation Clause"),
    _f("payment_timelines", "Payment Timelines", anchored=False),
    _f("show_bidder_documents",
       "Do you want to show documents uploaded by bidders to all bidders "
       "participated in bid?"),
    _f("min_bids_to_disable_extension",
       "Minimum number of bids required to disable automatic bid extension"),
    _f("auto_extension_days",
       "Number of days for which Bid would be auto-extended"),
    _f("auto_extension_count", "Number of Auto Extension count"),
)


@dataclass
class Found:
    """One label located in the flat text."""

    label: Label
    start: int
    end: int
    #: The section key governing this position, resolved after sorting.
    section: Optional[str] = None
    #: Raw text between this label and the next boundary. Filled by the parser.
    value: str = ""


def _is_anchored(flat: str, start: int, end: int,
                 line_starts: set[int], line_ends: set[int]) -> bool:
    """Does this match sit where a real label sits?

    A label in a GeM document occupies whole lines: it begins a line (or
    follows the slash that separated it from its Hindi twin) and runs to the
    end of a line, with its value on the lines after. Prose that happens to
    contain the same words does neither.

    That second half is what makes this safe. Every document carrying an
    estimated value also carries the sentence "Estimated Bid Value indicated
    above is being declared solely for the purpose of guidance..." in its
    boilerplate. The phrase matches; the position does not, because the
    sentence continues past the match on the same line. Requiring the match to
    *end a line* rejects it without needing to know anything about the prose.
    """
    if end not in line_ends:
        return False
    if start in line_starts:
        return True
    before = flat[:start].rstrip()
    return before.endswith("/")


def find_labels(
    flat: str,
    line_starts: Optional[set[int]] = None,
    line_ends: Optional[set[int]] = None,
    catalogue: tuple[Label, ...] = CATALOGUE,
) -> list[Found]:
    """Locate every catalogue label in ``flat``, resolved and ordered.

    Sections are resolved first and on their own. That ordering is load-bearing
    rather than tidy: ``Required`` is declared twice, once scoped to the EMD
    block and once to the ePBG block, and both declarations match the identical
    span of text. Resolving overlaps before sections would throw one of the two
    away as a duplicate, and the survivor would be whichever was declared first
    -- so an ePBG percentage would land on the EMD, or vanish. Sections first
    means each candidate is discarded or kept on the strength of *where it
    is*, and only the genuine survivors ever compete for a span.
    """
    if line_starts is None or line_ends is None:
        # Caller gave us no line map; fall back to slash-anchoring only, which
        # is what the bid-document form needs and is never wrong, just thinner.
        line_starts = line_starts if line_starts is not None else set()
        line_ends = line_ends if line_ends is not None else {len(flat)}

    def matches(labels: Iterable[Label]) -> list[Found]:
        out: list[Found] = []
        for label in labels:
            for rx in label.regexes:
                for m in rx.finditer(flat):
                    if label.anchored and not _is_anchored(
                        flat, m.start(), m.end(), line_starts, line_ends
                    ):
                        continue
                    out.append(Found(label=label, start=m.start(), end=m.end()))
        return out

    # 1. Section timeline. Sections do not nest, so a plain overlap sweep is
    #    enough here.
    sections = _resolve_overlaps(matches(s for s in catalogue if s.kind is Kind.SECTION))

    def section_at(pos: int) -> Optional[str]:
        current = None
        for s in sections:
            if s.start <= pos:
                current = s.label.key
            else:
                break
        return current

    # 2. Fields, filtered by the section they claim to belong to.
    fields: list[Found] = []
    for f in matches(l for l in catalogue if l.kind is Kind.FIELD):
        f.section = section_at(f.start)
        if f.label.section is not None and f.label.section != f.section:
            continue
        fields.append(f)

    # 3. Now, and only now, resolve overlaps among the survivors.
    resolved = _resolve_overlaps(fields)
    return sorted(sections + resolved, key=lambda f: (f.start, f.end))


def _resolve_overlaps(found: list[Found]) -> list[Found]:
    """Longest match wins; on a tie, the earlier one."""
    found = sorted(found, key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Found] = []
    for cand in found:
        clash = None
        for k in kept:
            if cand.start < k.end and k.start < cand.end:
                clash = k
                break
        if clash is None:
            kept.append(cand)
        elif (cand.end - cand.start) > (clash.end - clash.start):
            kept.remove(clash)
            kept.append(cand)
    return sorted(kept, key=lambda f: f.start)
