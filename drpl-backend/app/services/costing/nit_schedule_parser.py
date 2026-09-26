"""Deterministic parser for IREPS NIT bidding schedules.

The IREPS "2. SCHEDULE" section is a rigid token stream. This parser reads it
verbatim with zero LLM involvement so the tender's published numbers are
captured exactly. It is intentionally format-specific to IREPS; callers gate on
`is_ireps_schedule_format` and fall back to the AI/vision path otherwise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A schedule banner: "Schedule () A-PART 'A':- Repair…" or "Schedule () B7-…".
# IREPS sometimes prefixes the trailing title text with a "1000 more rows at
# the bottom" export artifact on the SAME logical line; that artifact is
# stripped from the captured title below (see _EXPORT_ARTIFACT_RE) so it
# doesn't leak into NITSchedule.title.
_BANNER_RE = re.compile(r"Schedule\s*\(\)\s*([A-Z]\d*)\s*-\s*(.*)")
# KNOWN HAZARD (deliberately not fixed here — see
# docs/superpowers/plans/2026-08-10-nit-item-breakup-schedule-capture.md
# "Out of scope"): `3. ITEM BREAKUP` is listed below as a section END, but in
# IREPS NITs it STARTS the region holding the real line items -- the same
# semantic inversion fixed in boq_parser_service._SCHEDULE_END_RE for the
# AI/vision path. This currently fails CLOSED: section-2 summary rows are
# 3-token, so _parse_lines emits zero NITLines, `det_items` is falsy, and
# parse_boq_from_tender falls through to the (fixed) AI path. A layout variant
# that made those rows parse would silently return a partial schedule set on
# the cheap path, which bypasses reconciliation entirely.
_SECTION_END_RE = re.compile(
    r"^\s*(3\.\s*ITEM\s*BREAKUP|No item break up added|4\.\s*ELIGIBILITY|"
    r"Special (Financial|Technical) Criteria|5\.\s*COMPLIANCE|General Instruction)",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"^-?\d[\d,]*\.?\d*$")
_INT_RE = re.compile(r"^\d+$")
# A rupee amount as IREPS prints it: two decimals. A schedule's stated total
# is one of these; a row's serial number ("1") is not, which is what lets the
# banner scan below tell the total from the first row.
_RUPEE_RE = re.compile(r"^\d[\d,]*\.\d{2}$")
# How many lines an Item Code may wrap onto. IREPS renders the code cell
# narrow, so "CONVERSION MAT" arrives as two lines and "PAINT MATERIAL" as
# two; nothing seen wraps past three.
_MAX_CODE_LINES = 3
# Longest line still plausible as (part of) an Item Code cell.
_MAX_CODE_CHARS = 32
# How many non-numeric lines may sit between a banner and its stated total
# ("(INCLUSIVE OF ALL TAXES AND CHARGES)", or the banner title's own
# continuation).
_MAX_BANNER_TRAILER_LINES = 3

# IREPS stamps a page header/footer on every rendered page. When a line item's
# Description:- continuation happens to straddle a page boundary, this
# boilerplate is interleaved into the token stream right alongside the real
# continuation text. Filter these lines out before/while collecting
# descriptions so they never pollute a line's description.
_BOILERPLATE_RE = re.compile(
    # Specific IREPS strings (kept verbatim)…
    r"^Run Date/Time:|"
    r"^TENDER DOCUMENT$|"
    r"^Tender No:|"
    r"^Closing Date/Time:|"
    r"^LOCO-SHOP-.*RLY$|"
    # …plus generalized page-number artifacts. Each alternative is fully
    # anchored so a real description that merely contains 'Page' is NOT matched.
    r"^Page\s+\d+(\s+of\s+\d+)?$|"   # "Page 4", "Page 4 of 12"
    r"^\d+\s*\|\s*Page$|"            # "4 | Page"
    r"^Page\s*\|\s*\d+$|"           # "Page | 4"
    r"^-\s*\d+\s*-$",               # "- 7 -" centered page number
    re.IGNORECASE,
)

# The IREPS export artifact that appears when a schedule has more rows than
# fit on the page: "1000 more rows at the bottom". It lands verbatim in the
# banner line ahead of the real title text, so strip it from the title.
_EXPORT_ARTIFACT_RE = re.compile(r"^\d+\s+more rows at the bottom\s*", re.IGNORECASE)


def _num(tok: str) -> float | None:
    t = (tok or "").replace(",", "").strip()
    if not _NUM_RE.match(tok.strip()):
        return None
    try:
        return float(t)
    except ValueError:
        return None


@dataclass(frozen=True)
class NITLine:
    schedule_code: str
    sr_no: int
    item_code: str
    description: str
    qty: float | None
    unit: str | None
    unit_rate: float | None
    basic_value: float | None
    escalation_pct: float
    amount: float | None
    bidding_unit: str | None


@dataclass(frozen=True)
class NITSchedule:
    code: str
    title: str
    stated_total: float | None
    lines: tuple[NITLine, ...]
    # The banner as printed, continuation lines included: "COST OF LABOUR:
    # ... (INCLUSIVE OF ALL TAXES AND CHARGES)". `title` is the banner's
    # first line only.
    full_title: str = ""


@dataclass(frozen=True)
class ParsedNIT:
    advertised_value: float | None
    schedules: tuple[NITSchedule, ...]


# How many lines a banner's title may continue onto. The Liluah Mid-Life NIT
# prints four ("... (INCLUSIVE OF ALL TAXES AND" / "CHARGES)").
_MAX_TITLE_LINES = 8
_MAX_TITLE_CHARS = 700
# IREPS repeats this instruction in every schedule banner; it says nothing
# about the schedule.
_TITLE_BOILERPLATE_RE = re.compile(
    r"Tenderer should submit break-?up of basic Rate and tax components with nature and "
    r"rate of tax etc\.? in a separate sheet along with the offer\.?\s*"
    r"(HSN and SAC Code should also be mentioned against each component\.?)?",
    re.IGNORECASE,
)


def _full_title(lines: list[str], banner_idx: int, first_line: str) -> str:
    """The banner's title with the lines it continues onto, up to the
    schedule's stated total or its first row."""
    parts = [first_line]
    j = banner_idx + 1
    while j < len(lines) and len(parts) <= _MAX_TITLE_LINES:
        ln = lines[j]
        if ln == "":
            j += 1
            continue
        if (
            _RUPEE_RE.match(ln) or _INT_RE.match(ln) or _NUM_RE.match(ln)
            or _BANNER_RE.search(ln) or _SECTION_END_RE.match(ln)
            or ln.startswith("Description:-") or _BOILERPLATE_RE.match(ln)
        ):
            break
        parts.append(ln)
        j += 1
    title = " ".join(" ".join(parts).split())
    title = " ".join(_TITLE_BOILERPLATE_RE.sub("", title).split())
    return title[:_MAX_TITLE_CHARS]


def schedule_titles(text: str) -> dict[str, str]:
    """{schedule code: full printed title} for an IREPS-format NIT's text."""
    return {s.code: (s.full_title or s.title) for s in parse_nit_text(text).schedules}


def is_ireps_schedule_format(text: str) -> bool:
    return bool(_BANNER_RE.search(text or "")) and "Item Qty" in (text or "")


def parse_nit_text(text: str) -> ParsedNIT:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    advertised_value = _find_advertised_value(lines)

    # Split the stream into schedule blocks at each banner; stop at section end.
    blocks: list[tuple[str, str, float | None, str, list[str]]] = []
    i = 0
    n = len(lines)
    current: tuple[str, str, float | None, str] | None = None
    body: list[str] = []
    while i < n:
        ln = lines[i]
        if _SECTION_END_RE.match(ln) and current is not None:
            blocks.append((*current, body))
            current, body = None, []
            break
        m = _BANNER_RE.search(ln)
        if m:
            if current is not None:
                blocks.append((*current, body))
            code = m.group(1).upper()
            title = _EXPORT_ARTIFACT_RE.sub("", m.group(2).strip()).strip()
            full_title = _full_title(lines, i, title)
            # The stated total is the first rupee amount after the banner.
            # It used to have to be the very next line, but the Liluah NIT
            # prints the title's continuation and "(INCLUSIVE OF ALL TAXES
            # AND CHARGES)" between the two, so both schedules lost their
            # total and the reconciliation gate had nothing to check against.
            # The scan stops at the first row start (a bare integer) so a
            # schedule that prints no total never takes a row's amount as one.
            stated = None
            j = i + 1
            trailer = 0
            while j < n and trailer <= _MAX_BANNER_TRAILER_LINES:
                ln_j = lines[j]
                if ln_j == "":
                    j += 1
                    continue
                if _RUPEE_RE.match(ln_j):
                    stated = _num(ln_j)
                    i = j
                    break
                if _INT_RE.match(ln_j) or _BANNER_RE.search(ln_j) or _SECTION_END_RE.match(ln_j):
                    break
                trailer += 1
                j += 1
            current, body = (code, title, stated, full_title), []
            i += 1
            continue
        if current is not None:
            body.append(ln)
        i += 1
    if current is not None:
        blocks.append((*current, body))

    schedules = tuple(
        NITSchedule(code=code, title=title, stated_total=stated,
                    lines=tuple(_parse_lines(code, body)), full_title=full_title)
        for (code, title, stated, full_title, body) in blocks
    )
    return ParsedNIT(advertised_value=advertised_value, schedules=schedules)


def _find_advertised_value(lines: list[str]) -> float | None:
    for idx, ln in enumerate(lines):
        if ln.lower() == "advertised value":
            for k in range(idx + 1, min(idx + 4, len(lines))):
                v = _num(lines[k])
                if v is not None:
                    return v
    return None


# A unit and the rate beside it, rendered as one line: "Set 223465.00". The
# text extractor joins two cells when their glyphs sit close enough, and it
# happens on a few rows per NIT (five of ninety-seven on one), never on all.
# Only a short word run followed by a rupee amount qualifies; a description
# line is never this short and never starts with a unit.
_MERGED_UNIT_RATE_RE = re.compile(r"^([A-Za-z][A-Za-z .\-/]{0,24}?)\s+(\d[\d,]*\.\d{2})$")


def _split_merged_cells(body: list[str]) -> list[str]:
    out: list[str] = []
    for tok in body:
        m = _MERGED_UNIT_RATE_RE.match(tok)
        if m and not tok.startswith("Description:-"):
            out.append(m.group(1).strip())
            out.append(m.group(2))
        else:
            out.append(tok)
    return out


# How far past a serial-less field run its displaced serial may reappear: the
# "Rs." cell plus one page's header/footer stamp.
_MAX_DISPLACED_SERIAL_GAP = 12


def _field_run(body: list[str], start: int) -> tuple | None:
    """The fixed field run of a row whose Item Code starts at `start`:
    (qty_index, item_code, qty, unit, unit_rate, basic_value, escl_tok, amount),
    or None when no run of that shape starts there.

    The Item Code cell is rendered narrow, so a code like "CONVERSION MAT"
    arrives as two lines. This used to read a fixed eight-token run, so every
    wrapped code shifted the fields by one, failed the numeric checks, and the
    row was skipped -- ten of the Liluah NIT's sixteen rows, including the two
    that cite the annexures. The run starts at the first token that reads as a
    quantity followed by a word and three amounts; the tokens before it are
    the code.
    """
    n = len(body)
    # width 0: the Item Code cell was pushed past a page break, so the
    # quantity follows the serial directly and the code turns up after
    # the page boilerplate, as a bare integer equal to the serial (row 90
    # of NIT 5374229). The description scan picks it up.
    for width in range(0, _MAX_CODE_LINES + 1):
        base = start + width  # index of qty
        if base + 5 >= n:
            break
        qty = _num(body[base])
        unit = body[base + 1]
        unit_rate = _num(body[base + 2])
        basic_value = _num(body[base + 3])
        escl_tok = body[base + 4]
        amount = _num(body[base + 5])
        # Validate the shape: qty & rate & amount numeric, unit is a word,
        # and the code lines are words (a wrapped code never contains a
        # bare amount).
        if qty is None or unit_rate is None or amount is None or _NUM_RE.match(unit):
            continue
        code_lines = body[start:base]
        # A code is a short word or two ("STRIPPING", "CONVERSION MAT",
        # "a", "01"). A wrapped code never contains a bare amount, and no
        # code is a description line or a page-boilerplate line -- the
        # search must not reach past an unreadable row into the next
        # row's fields (Parel B-43 swallowed B-44 that way).
        if any(
            (width > 1 and _NUM_RE.match(c))
            or c.startswith("Description:-")
            or _BOILERPLATE_RE.match(c)
            or len(c) > _MAX_CODE_CHARS
            for c in code_lines
        ):
            continue
        return (base, " ".join(c.strip() for c in code_lines if c.strip()),
                qty, unit, unit_rate, basic_value, escl_tok, amount)
    return None


def _whole_field_run(body: list[str], start: int) -> tuple | None:
    """`_field_run`, held to the arithmetic every IREPS row prints: basic
    value = qty x rate. Used where no serial vouches for the run (a row
    whose serial was displaced, or the end of a description), because
    there a description's last line, the next serial and the next row's
    first cells can happen to fit the shape: "required for SBC." "7"
    "Elect" "2.00" read as code, qty 7, unit Elect, rate 2.00."""
    found = _field_run(body, start)
    if found is None:
        return None
    _, _, qty, _, unit_rate, basic_value, _, _ = found
    if basic_value is None or abs(qty * unit_rate - basic_value) > 1.0:
        return None
    return found


def _displaced_serial(body: list[str], after: int) -> tuple[int, int] | None:
    """(serial, index after it) when a serial-less field run's serial was
    pushed past a page break: the next bare integer, within
    `_MAX_DISPLACED_SERIAL_GAP` tokens of noise, printed directly before a
    "Description:-" line. None otherwise."""
    k, n = after, len(body)
    while k < n and k - after <= _MAX_DISPLACED_SERIAL_GAP:
        t = body[k]
        if t.startswith("Description:-") or _BANNER_RE.search(t):
            return None
        if _INT_RE.match(t):
            m = k + 1
            while m < n and body[m] == "":
                m += 1
            return (int(t), m) if m < n and body[m].startswith("Description:-") else None
        k += 1
    return None


def _parse_lines(schedule_code: str, body: list[str]) -> list[NITLine]:
    """A row is: sr_no(int), item_code, qty(num), unit(word), unit_rate(num),
    basic_value(num), escl-or-'AT Par', amount(num), then 'Description:- …'.
    We scan for the sr_no+item_code+qty anchor and read the fixed field run."""
    out: list[NITLine] = []
    body = _split_merged_cells(body)
    i, n = 0, len(body)
    while i < n:
        if _INT_RE.match(body[i]):
            sr_no = int(body[i])
            found = _field_run(body, i + 1)
            if found is None:
                i += 1
                continue
            j = found[0] + 6
        else:
            # header rows ("S.No.", "Item", "Code", …) and blanks are skipped,
            # unless a row's fields start here with its serial displaced: a
            # page break fell between the serial cell and the rest, so the
            # fields print at the foot of one page and the serial reappears
            # on the next, just before the description (Mid-Life NIT,
            # schedule R row 6 -- the only row of 286 the parser lost, and
            # losing it sent the whole NIT to the AI extractor).
            found = _whole_field_run(body, i)
            displaced = _displaced_serial(body, found[0] + 6) if found else None
            if displaced is None:
                i += 1
                continue
            sr_no, j = displaced
        base, item_code, qty, unit, unit_rate, basic_value, escl_tok, amount = found
        # NOTE: IREPS "AT Par" rows are 8-token (sr_no, item_code, qty, unit,
        # unit_rate, basic_value, escl-or-bidding-unit, amount) with the escl
        # slot holding either a numeric escalation % or a text bidding unit
        # (e.g. "AT Par"). This fixture only exercises the "AT Par" text case
        # with escl_pct always 0.0; a general escl+bidding tokenizer for other
        # IREPS formats is out of scope here. If a row's fixed field-run
        # doesn't parse cleanly (e.g. a non-numeric qty/rate/amount), it is
        # skipped above without emitting a corrupted line — a dropped or
        # misparsed line is caught downstream by the reconciliation gate
        # (schedule line-amount sum won't equal the stated total -> FLAG), so
        # no fabrication or silent data loss escapes review.
        escl_pct = 0.0 if not _NUM_RE.match(escl_tok) else (_num(escl_tok) or 0.0)
        bidding_unit = escl_tok if not _NUM_RE.match(escl_tok) else "AT Par"
        # Collect the Description:- continuation lines.
        desc_parts: list[str] = []
        while j < n:
            t = body[j]
            if _INT_RE.match(t):
                # A displaced Item Code (see width 0 above): the serial
                # printed again, before the description, on a row that had
                # no code. Anything else that is a bare integer is the next
                # row's serial.
                if not item_code and not desc_parts and int(t) == sr_no:
                    item_code = t
                    j += 1
                    continue
                break
            if _BOILERPLATE_RE.match(t):
                j += 1
                continue
            if t.startswith("Description:-"):
                desc_parts.append(t[len("Description:-"):].strip())
            elif desc_parts:
                # The next row's fields, its serial displaced past a page
                # break, end this description; read as text they swallowed
                # that row whole.
                if _whole_field_run(body, j) is not None:
                    break
                desc_parts.append(t)
            j += 1
        out.append(NITLine(
            schedule_code=schedule_code, sr_no=sr_no, item_code=item_code.strip(),
            description=" ".join(desc_parts).strip(), qty=qty, unit=unit.strip(),
            unit_rate=unit_rate, basic_value=basic_value, escalation_pct=escl_pct,
            amount=amount, bidding_unit=bidding_unit,
        ))
        i = j
    return out
