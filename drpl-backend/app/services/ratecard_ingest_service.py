"""
DRPL Backend - Ratecard Ingestion Service

Deterministic Excel parser + CRUD + lookup for structured ratecards.

The ratecard sheets observed in the wild have this shape (one tab, top to
bottom):

    Annexure-A                                  <- annexure banner (optional)
    D check Manadatory Spares - VTA 28 L ...    <- check/section banner
    S.N. | Part No. | Part Desc | Qty | Our Rate | Rate | Remark   <- header
    1 | KIT3238515 | B CHECK KIT ... | 1 | 4908 | =E6*D6 | Fleetguard  <- data
    ...
    Total | ... | =SUM(...)                     <- subtotal (skipped)

    NTA855R 'C' Check Schedule                  <- check banner (engine+level)
    Sr.No | Part No | Description | Qty | Rate (2026) | Total | Remark
    ...

This parser is a small row-state machine: it tracks the current annexure,
current engine type, current check level (B/C/D), and current
mandatory/optional flag, and emits a RatecardItem per data row keyed by a
drift-tolerant normalised part number.

No AI call — the structure is deterministic. Use template_parser_xlsx.py's
AI path only for free-form template inference, not for these price lists.
"""

import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from openpyxl import load_workbook
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.ratecard import Ratecard, RatecardItem, RatecardCheckSchedule

logger = logging.getLogger(__name__)

ALLOWED_RATECARD_EXTENSIONS = {".xlsx", ".xlsm"}
MAX_RATECARD_FILE_SIZE = 15 * 1024 * 1024  # 15 MB


# ──────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────

# Engine types seen across the ratecards/costing sheets. Order matters: match
# the most specific spelling first. The capture is normalised to a canonical
# label via _ENGINE_CANON.
_ENGINE_PATTERNS = [
    (re.compile(r"\bN\s*T\s*A?\s*[- ]?\s*855\s*R?\b", re.I), "NTA 855R"),
    (re.compile(r"\bV\s*T\s*A?\s*[- ]?\s*28\s*L?\b", re.I), "VTA 28L"),
    (re.compile(r"\bV\s*T\s*A?\s*[- ]?\s*1710\s*L?\b", re.I), "VTA 1710L"),
]

# Check level can appear as "D check", "D-check", "'C' Check", '"D" CHECK',
# or "B and C-Check" (we take the last letter for the kit when a range is given).
_CHECK_RE = re.compile(r"['\"]?\b([BCD])\b['\"]?\s*[-/ ]?\s*check\b", re.I)
_CHECK_SCHEDULE_RE = re.compile(r"check\s+schedule", re.I)
_ANNEXURE_RE = re.compile(r"\bannexure\s*[-:]?\s*([A-L])\b", re.I)
_MANDATORY_RE = re.compile(r"\bmandatory\b", re.I)
_OPTIONAL_RE = re.compile(r"\b(optional|needs\s*basis)\b", re.I)

# Header column synonyms -> canonical key.
_HEADER_SYNONYMS = {
    "part_no": {"part no", "part no.", "partno", "partno.", "part number"},
    "description": {"part desc", "description", "desc", "part description"},
    "qty": {"qty", "quantity", "qty / engine", "qty/engine"},
    "rate": {"our rate", "rate", "rate (2026)", "unit rate", "rly. sup. rate"},
    "uom": {"uom", "unit", "units"},
    "remark": {"remark", "remarks", "source"},
    "sr_no": {"s.n.", "sn", "sr.no", "sr no", "sr. no.", "sl no", "sr.no.", "s. no."},
}


def _norm_part(s) -> str:
    """Drift-tolerant exact-match key: uppercase, alnum-only."""
    if s is None:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(s)).upper()


def _norm_header(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _detect_engine(text: str) -> Optional[str]:
    for pat, canon in _ENGINE_PATTERNS:
        if pat.search(text or ""):
            return canon
    return None


def _row_text(values) -> str:
    # Normalise non-breaking spaces so \b word boundaries behave.
    return " ".join(
        str(v).replace("\xa0", " ") for v in values
        if v is not None and str(v).strip()
    )


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    # Strip currency symbols / stray text.
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in {"-", ".", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_header_row(values) -> Optional[dict]:
    """If this row looks like a column header, return {canonical_key: col_idx}."""
    col_map: dict[str, int] = {}
    matched = 0
    for idx, v in enumerate(values):
        token = _norm_header(v)
        if not token:
            continue
        for key, synonyms in _HEADER_SYNONYMS.items():
            if token in synonyms and key not in col_map:
                col_map[key] = idx
                matched += 1
                break
    # A real header has at least a part_no/description + a rate column.
    if matched >= 3 and ("part_no" in col_map or "description" in col_map) and "rate" in col_map:
        return col_map
    return None


# ──────────────────────────────────────────────
# Core parse
# ──────────────────────────────────────────────

def _parse_workbook(file_bytes: bytes) -> dict:
    """
    Parse the workbook into a structured dict (no DB writes):
        {
          "check_schedules": [{key, engine_type, check_level, name}],
          "items": [{part_no, part_no_norm, description, qty, rate, uom,
                     source, remark, engine_type, check_key, is_mandatory,
                     annexure, sr_no}],
          "skipped": [str, ...],
        }
    check_key links an item to its check schedule (by index in check_schedules),
    or None.
    """
    wb = load_workbook(io.BytesIO(file_bytes), data_only=True)

    check_schedules: list[dict] = []
    check_index: dict[tuple, int] = {}   # (engine_type, check_level, name) -> idx
    items: list[dict] = []
    skipped: list[str] = []

    for ws in wb.worksheets:
        col_map: Optional[dict] = None
        cur_engine: Optional[str] = None
        cur_check_level: Optional[str] = None
        cur_check_key: Optional[int] = None
        cur_annexure: Optional[str] = None
        cur_mandatory: bool = True

        for row in ws.iter_rows(values_only=True):
            if row is None:
                continue
            values = list(row)
            text = _row_text(values)
            if not text:
                continue

            low = text.lower()

            # Subtotal/total rows — skip.
            if low.startswith("total") or "=sum(" in low or low.strip() == "total":
                continue

            # Annexure banner.
            m_ann = _ANNEXURE_RE.search(text)
            if m_ann and len(text) < 40:
                cur_annexure = m_ann.group(1).upper()
                # an annexure line alone carries no data
                continue

            # Header row.
            maybe_header = _is_header_row(values)
            if maybe_header:
                col_map = maybe_header
                continue

            # Section / check banner: a mostly-text row that names an engine
            # and/or a check level, sitting outside the data columns.
            eng = _detect_engine(text)
            m_check = _CHECK_RE.search(text)
            is_schedule_banner = bool(_CHECK_SCHEDULE_RE.search(text))
            looks_like_banner = (
                (eng or m_check or is_schedule_banner)
                and not (col_map and _looks_like_data_row(values, col_map))
            )
            if looks_like_banner:
                if eng:
                    cur_engine = eng
                if m_check:
                    cur_check_level = m_check.group(1).upper()
                # Track mandatory/optional context from the banner text.
                if _OPTIONAL_RE.search(text):
                    cur_mandatory = False
                elif _MANDATORY_RE.search(text):
                    cur_mandatory = True
                # Register a check schedule when we have an engine + level.
                if cur_engine and cur_check_level:
                    key = (cur_engine, cur_check_level, text.strip()[:255])
                    if key not in check_index:
                        check_index[key] = len(check_schedules)
                        check_schedules.append({
                            "engine_type": cur_engine,
                            "check_level": cur_check_level,
                            "name": text.strip()[:255],
                        })
                    cur_check_key = check_index[key]
                continue

            # Data row — needs a header established.
            if not col_map:
                continue
            if not _looks_like_data_row(values, col_map):
                continue

            part_no = _cell(values, col_map.get("part_no"))
            description = _cell(values, col_map.get("description"))
            rate = _to_float(_cell(values, col_map.get("rate")))
            if not (part_no or description):
                continue

            items.append({
                "part_no": (str(part_no).strip() if part_no else None),
                "part_no_norm": _norm_part(part_no),
                "description": (str(description).strip() if description else None),
                "qty": _to_float(_cell(values, col_map.get("qty"))) or 1.0,
                "rate": rate,
                "uom": _cell_str(values, col_map.get("uom")),
                "source": _cell_str(values, col_map.get("remark")),
                "remark": _cell_str(values, col_map.get("remark")),
                "engine_type": cur_engine,
                "check_key": cur_check_key,
                "is_mandatory": cur_mandatory,
                "annexure": cur_annexure,
                "sr_no": _to_int(_cell(values, col_map.get("sr_no"))),
            })

    return {"check_schedules": check_schedules, "items": items, "skipped": skipped}


def _cell(values, idx):
    if idx is None or idx >= len(values):
        return None
    return values[idx]


def _cell_str(values, idx):
    v = _cell(values, idx)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _to_int(v):
    f = _to_float(v)
    return int(f) if f is not None else None


def _looks_like_data_row(values, col_map) -> bool:
    """A data row has a part_no or description AND a numeric-ish rate/qty cell."""
    part = _cell(values, col_map.get("part_no"))
    desc = _cell(values, col_map.get("description"))
    if not (part or desc):
        return False
    rate = _to_float(_cell(values, col_map.get("rate")))
    qty = _to_float(_cell(values, col_map.get("qty")))
    return rate is not None or qty is not None


# ──────────────────────────────────────────────
# Public ingestion API
# ──────────────────────────────────────────────

def ingest_ratecard_xlsx(
    db: Session, ratecard_id: int, file_bytes: bytes, file_name: str,
) -> dict:
    """Parse an Excel ratecard and persist its items + check schedules."""
    ratecard = db.query(Ratecard).filter(Ratecard.id == ratecard_id).first()
    if not ratecard:
        raise ValueError(f"Ratecard {ratecard_id} not found")

    parsed = _parse_workbook(file_bytes)

    # Persist check schedules first so items can reference them.
    schedule_rows: list[RatecardCheckSchedule] = []
    for cs in parsed["check_schedules"]:
        row = RatecardCheckSchedule(
            ratecard_id=ratecard_id,
            engine_type=cs["engine_type"],
            check_level=cs["check_level"],
            name=cs["name"],
        )
        db.add(row)
        schedule_rows.append(row)
    db.flush()  # assign ids

    n_items = 0
    for it in parsed["items"]:
        check_id = None
        if it["check_key"] is not None and it["check_key"] < len(schedule_rows):
            check_id = schedule_rows[it["check_key"]].id
        db.add(RatecardItem(
            ratecard_id=ratecard_id,
            check_schedule_id=check_id,
            engine_type=it["engine_type"],
            part_no=it["part_no"],
            part_no_norm=it["part_no_norm"] or None,
            description=it["description"],
            uom=it["uom"],
            qty=it["qty"],
            rate=it["rate"],
            source=(it["source"][:64] if it["source"] else ratecard.source_label),
            remark=it["remark"],
            is_mandatory=it["is_mandatory"],
            annexure=it["annexure"],
            sr_no=it["sr_no"],
        ))
        n_items += 1

    ratecard.original_file_name = file_name
    ratecard.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        f"Ingested ratecard {ratecard_id} from {file_name}: "
        f"{n_items} items, {len(schedule_rows)} check schedules"
    )
    return {
        "ratecard_id": ratecard_id,
        "items": n_items,
        "check_schedules": len(schedule_rows),
        "skipped": parsed["skipped"],
    }


def preview_ratecard_xlsx(file_bytes: bytes) -> dict:
    """Parse without committing — for the upload-preview UI."""
    parsed = _parse_workbook(file_bytes)
    return {
        "item_count": len(parsed["items"]),
        "check_schedule_count": len(parsed["check_schedules"]),
        "check_schedules": parsed["check_schedules"][:50],
        "sample_items": parsed["items"][:25],
        "skipped": parsed["skipped"],
    }


# ──────────────────────────────────────────────
# Ratecard CRUD
# ──────────────────────────────────────────────

def create_ratecard(
    db: Session,
    name: str,
    source_label: Optional[str] = None,
    description: Optional[str] = None,
    created_by: Optional[int] = None,
) -> Ratecard:
    rc = Ratecard(
        name=name.strip(),
        source_label=(source_label.strip() if source_label else None),
        description=description,
        created_by=created_by,
    )
    try:
        db.add(rc)
        db.commit()
        db.refresh(rc)
    except IntegrityError:
        db.rollback()
        raise ValueError(f"A ratecard named '{name}' already exists")
    return rc


def list_ratecards(db: Session, status_filter: Optional[str] = None) -> list[dict]:
    from sqlalchemy import func

    q = db.query(Ratecard)
    if status_filter:
        q = q.filter(Ratecard.status == status_filter)
    out = []
    for rc in q.order_by(Ratecard.created_at.desc()).all():
        item_count = db.query(func.count(RatecardItem.id)).filter(
            RatecardItem.ratecard_id == rc.id
        ).scalar() or 0
        sched_count = db.query(func.count(RatecardCheckSchedule.id)).filter(
            RatecardCheckSchedule.ratecard_id == rc.id
        ).scalar() or 0
        out.append({
            "id": rc.id,
            "name": rc.name,
            "source_label": rc.source_label,
            "description": rc.description,
            "status": rc.status,
            "original_file_name": rc.original_file_name,
            "item_count": item_count,
            "check_schedule_count": sched_count,
            "created_at": rc.created_at.isoformat() if rc.created_at else None,
            "updated_at": rc.updated_at.isoformat() if rc.updated_at else None,
        })
    return out


def get_ratecard_with_items(db: Session, ratecard_id: int) -> Optional[dict]:
    from sqlalchemy import func

    rc = db.query(Ratecard).filter(Ratecard.id == ratecard_id).first()
    if not rc:
        return None
    item_count = db.query(func.count(RatecardItem.id)).filter(
        RatecardItem.ratecard_id == rc.id
    ).scalar() or 0
    schedules = db.query(RatecardCheckSchedule).filter(
        RatecardCheckSchedule.ratecard_id == rc.id
    ).all()
    return {
        "id": rc.id,
        "name": rc.name,
        "source_label": rc.source_label,
        "description": rc.description,
        "status": rc.status,
        "original_file_name": rc.original_file_name,
        "item_count": item_count,
        "check_schedules": [
            {
                "id": s.id,
                "engine_type": s.engine_type,
                "check_level": s.check_level,
                "name": s.name,
            }
            for s in schedules
        ],
    }


def list_items(
    db: Session,
    ratecard_id: int,
    engine_type: Optional[str] = None,
    check_level: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    q = db.query(RatecardItem).filter(RatecardItem.ratecard_id == ratecard_id)
    if engine_type:
        q = q.filter(RatecardItem.engine_type == engine_type)
    if check_level:
        q = (
            q.join(RatecardCheckSchedule,
                   RatecardItem.check_schedule_id == RatecardCheckSchedule.id)
            .filter(RatecardCheckSchedule.check_level == check_level.upper())
        )
    rows = q.order_by(RatecardItem.id.asc()).offset(offset).limit(limit).all()
    return [_item_dict(r) for r in rows]


def delete_ratecard(db: Session, ratecard_id: int) -> bool:
    rc = db.query(Ratecard).filter(Ratecard.id == ratecard_id).first()
    if not rc:
        return False
    db.delete(rc)  # cascade deletes items + check schedules
    db.commit()
    return True


def delete_item(db: Session, item_id: int) -> bool:
    it = db.query(RatecardItem).filter(RatecardItem.id == item_id).first()
    if not it:
        return False
    db.delete(it)
    db.commit()
    return True


# ──────────────────────────────────────────────
# Lookup API (used by the ratecard_lookup tool + orchestrator)
# ──────────────────────────────────────────────

def _item_dict(r: RatecardItem) -> dict:
    return {
        "part_no": r.part_no,
        "description": r.description,
        "qty": r.qty,
        "uom": r.uom,
        "rate": r.rate,
        "source": r.source,
        "engine_type": r.engine_type,
        "is_mandatory": r.is_mandatory,
        "annexure": r.annexure,
    }


def lookup_by_part_no(
    db: Session, part_no: str, engine_type: Optional[str] = None
) -> list[dict]:
    """Exact lookup via the normalised part number."""
    norm = _norm_part(part_no)
    if not norm:
        return []
    q = db.query(RatecardItem).filter(RatecardItem.part_no_norm == norm)
    if engine_type:
        q = q.filter(RatecardItem.engine_type == engine_type)
    return [_item_dict(r) for r in q.limit(20).all()]


def fuzzy_lookup(
    db: Session,
    query: str,
    engine_type: Optional[str] = None,
    max_results: int = 8,
) -> list[dict]:
    """Term-overlap search over description + part_no."""
    query_terms = [t for t in re.split(r"\s+", query.lower().strip()) if len(t) > 2]
    if not query_terms:
        return []
    q = db.query(RatecardItem)
    if engine_type:
        q = q.filter(RatecardItem.engine_type == engine_type)

    scored = []
    for r in q.limit(5000).all():
        hay = f"{r.part_no or ''} {r.description or ''}".lower()
        matches = sum(1 for t in query_terms if t in hay)
        if matches >= max(1, len(query_terms) // 2):
            d = _item_dict(r)
            d["relevance_score"] = matches
            scored.append(d)
    scored.sort(key=lambda x: x["relevance_score"], reverse=True)
    return scored[:max_results]


def expand_check_schedule(
    db: Session,
    engine_type: str,
    check_level: str,
    include_optional: bool = True,
) -> list[dict]:
    """
    Return every spare part belonging to the (engine_type, check_level) kit.
    Matches the most recently ingested schedule when several share the key.
    """
    q = (
        db.query(RatecardCheckSchedule)
        .filter(RatecardCheckSchedule.engine_type == engine_type)
        .filter(RatecardCheckSchedule.check_level == (check_level or "").upper())
        .order_by(RatecardCheckSchedule.id.desc())
    )
    schedules = q.all()
    if not schedules:
        return []
    sched_ids = [s.id for s in schedules]
    iq = db.query(RatecardItem).filter(RatecardItem.check_schedule_id.in_(sched_ids))
    if not include_optional:
        iq = iq.filter(RatecardItem.is_mandatory.is_(True))
    return [_item_dict(r) for r in iq.order_by(RatecardItem.id.asc()).all()]
