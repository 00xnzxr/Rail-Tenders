"""
DRPL Backend - Tender Service
Handles tender ingestion, deduplication, and querying
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
import re

from app.models.tender import Tender, ScrapeLog
from app.schemas import TenderInput, BatchUploadResponse, ScrapeStatusInput


# Markers that identify a corrupt scraped title (see plan Global Constraints).
_TENDER_TYPE_SUFFIX_RE = re.compile(r"\s*Tender\s*Type\s*:.*$", re.IGNORECASE)
_TRAILING_ELLIPSIS_RE = re.compile(r"[.…]{2,}\s*$")


def _trunc(v: "str | None", n: int) -> "str | None":
    """Truncate a scraped string to a column's width, preserving None.

    Postgres rejects an over-length VARCHAR insert outright (unlike SQLite,
    which doesn't enforce width), and the per-tender SAVEPOINT in
    ingest_tender_batch silently drops that tender on failure. The extension
    detail-page regexes can produce strings longer than the DB column limits,
    so every card string field must be clamped before it reaches the model.
    """
    if not v:
        return None
    return v[:n]


def clean_tender_title(raw: "str | None") -> str:
    """Normalize a scraped tender title: drop NBSP, the embedded 'Tender Type:'
    suffix, and trailing ellipsis runs, then collapse whitespace. Pure string fn."""
    if not raw:
        return ""
    s = raw.replace("\xa0", " ")
    s = _TENDER_TYPE_SUFFIX_RE.sub("", s)
    s = _TRAILING_ELLIPSIS_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_corrupt_title(title: "str | None") -> bool:
    """True if the title shows scrape-corruption markers and should not be trusted."""
    if not title:
        return False
    if "\xa0" in title:
        return True
    # Only TRAILING ellipsis is a corruption marker — it mirrors what
    # clean_tender_title strips. A legit mid-string "..." (e.g. "Supply of ...
    # bolts") is NOT corrupt, so it isn't silently dropped to a placeholder.
    if _TRAILING_ELLIPSIS_RE.search(title):
        return True
    if re.search(r"Tender\s*Type\s*:", title, re.IGNORECASE):
        return True
    if re.match(r"\s*Quantity\s*:", title, re.IGNORECASE):
        return True
    return False


def ingest_tender_batch(
    db: Session,
    tenders: list[TenderInput],
    user_id: int,
) -> BatchUploadResponse:
    """
    Process a batch of tenders from the Chrome extension.
    Deduplicates against existing records and inserts new ones.
    """
    received = len(tenders)
    new_count = 0
    duplicate_count = 0
    error_count = 0
    new_ids: list[int] = []

    for tender_input in tenders:
        try:
            # Use SAVEPOINT so a single failure doesn't kill the whole batch
            nested = db.begin_nested()

            # Check for existing tender (portal + tender_id is unique)
            existing = db.query(Tender).filter(
                and_(
                    Tender.portal == tender_input.portal,
                    Tender.tender_id == tender_input.tenderId,
                )
            ).first()

            if existing:
                # Update if status or dates changed
                _update_existing_tender(existing, tender_input)
                # Phase 7 — refresh scope-driven flags if new info arrives
                if getattr(tender_input, "searchMatchKeyword", None) and not existing.search_match_keyword:
                    existing.search_match_keyword = tender_input.searchMatchKeyword
                ind = getattr(tender_input, "isEligibleIndicator", None)
                if ind is not None and existing.is_eligible_indicator is None:
                    existing.is_eligible_indicator = ind
                duplicate_count += 1
            else:
                # Clean + ensure title is not empty (DB requires non-null)
                title = clean_tender_title(tender_input.title)
                if not title or is_corrupt_title(title):
                    title = f"{tender_input.portal} #{tender_input.tenderId}"

                # Insert new tender
                new_tender = Tender(
                    portal=tender_input.portal,
                    tender_id=tender_input.tenderId,
                    title=title,
                    department=tender_input.department or "",
                    organisation=tender_input.organisation or "",
                    description=tender_input.description or "",
                    estimated_value=tender_input.estimatedValue,
                    currency=tender_input.currency or "INR",
                    opening_date=_parse_date(tender_input.openingDate),
                    closing_date=_parse_date(tender_input.closingDate),
                    emd_amount=tender_input.emdAmount,
                    pre_bid_date=_parse_date(tender_input.preBidDate),
                    status=(tender_input.status or "open").lower(),
                    document_links=tender_input.documentLinks or [],
                    source_url=tender_input.sourceUrl or "",
                    extracted_by=user_id,
                    extracted_at=_parse_date(tender_input.extractedAt) or datetime.now(timezone.utc),
                    # Deep scrape fields
                    detail_url=tender_input.detailUrl or "",
                    full_description=tender_input.fullDescription or "",
                    eligibility_criteria=tender_input.eligibilityCriteria or "",
                    technical_specifications=tender_input.technicalSpecifications or "",
                    evaluation_criteria=tender_input.evaluationCriteria or "",
                    performance_guarantee=tender_input.performanceGuarantee,
                    performance_guarantee_percent=tender_input.performanceGuaranteePercent,
                    pre_bid_meeting_location=tender_input.preBidMeetingLocation or "",
                    delivery_location=tender_input.deliveryLocation or "",
                    delivery_timeline=tender_input.deliveryTimeline or "",
                    buyer_contact_name=tender_input.buyerContactName or "",
                    buyer_contact_email=tender_input.buyerContactEmail or "",
                    buyer_contact_phone=tender_input.buyerContactPhone or "",
                    number_of_amendments=tender_input.numberOfAmendments or 0,
                    corrigenda_links=tender_input.corrigendaLinks or [],
                    nit_document_links=tender_input.nitDocumentLinks or [],
                    amendment_links=tender_input.amendmentLinks or [],
                    is_detail_extracted=tender_input.isDetailExtracted,
                    # Phase 7 — scope-driven scraping
                    search_match_keyword=getattr(tender_input, "searchMatchKeyword", None),
                    is_eligible_indicator=getattr(tender_input, "isEligibleIndicator", None),
                    # Detailed-card fields (2026-07) — truncated to column width; see _trunc.
                    location=_trunc(tender_input.location, 255),
                    bid_type=_trunc(tender_input.bidType, 50),
                    source_portal=_trunc(tender_input.sourcePortal, 50),
                    category=_trunc(tender_input.category, 255),
                )
                db.add(new_tender)
                db.flush()  # populate id for fan-out scoring
                new_ids.append(new_tender.id)
                new_count += 1

            # Release SAVEPOINT — keeps changes in the outer transaction
            nested.commit()

        except Exception as e:
            # Roll back ONLY this savepoint, not the entire transaction
            nested.rollback()
            print(f"[DRPL] Error ingesting tender {tender_input.tenderId}: {e}")
            error_count += 1

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[DRPL] Batch commit error: {e}")
        return BatchUploadResponse(
            received=received,
            new=0,
            duplicates=0,
            errors=received,
            new_ids=[],
        )

    return BatchUploadResponse(
        received=received,
        new=new_count,
        duplicates=duplicate_count,
        errors=error_count,
        new_ids=new_ids,
    )


def _update_existing_tender(existing: Tender, new_data: TenderInput):
    """Update an existing tender if meaningful fields have changed.
    When isDetailExtracted is True, this is an enrichment update from a detail page —
    overwrite fields with richer data.
    """
    # Repair / upgrade the stored title when a clearly-better one arrives, so a
    # re-scrape with the fixed extension self-heals corrupt rows. Never downgrade
    # a good title to a shorter/equal one.
    new_clean = clean_tender_title(getattr(new_data, "title", None))
    if new_clean and not is_corrupt_title(new_clean):
        if is_corrupt_title(existing.title) or len(new_clean) > len(existing.title or ""):
            existing.title = new_clean

    if new_data.status and new_data.status.lower() != existing.status:
        existing.status = new_data.status.lower()
    if new_data.closingDate:
        new_close = _parse_date(new_data.closingDate)
        if new_close and new_close != existing.closing_date:
            existing.closing_date = new_close
    if new_data.documentLinks and len(new_data.documentLinks) > len(existing.document_links or []):
        existing.document_links = new_data.documentLinks

    # Deep scrape enrichment — overwrite when detail page data arrives
    if new_data.isDetailExtracted and not existing.is_detail_extracted:
        existing.is_detail_extracted = True
        if new_data.detailUrl:
            existing.detail_url = new_data.detailUrl
        if new_data.fullDescription:
            existing.full_description = new_data.fullDescription
        if new_data.eligibilityCriteria:
            existing.eligibility_criteria = new_data.eligibilityCriteria
        if new_data.technicalSpecifications:
            existing.technical_specifications = new_data.technicalSpecifications
        if new_data.evaluationCriteria:
            existing.evaluation_criteria = new_data.evaluationCriteria
        if new_data.performanceGuarantee is not None:
            existing.performance_guarantee = new_data.performanceGuarantee
        if new_data.performanceGuaranteePercent is not None:
            existing.performance_guarantee_percent = new_data.performanceGuaranteePercent
        if new_data.preBidMeetingLocation:
            existing.pre_bid_meeting_location = new_data.preBidMeetingLocation
        if new_data.deliveryLocation:
            existing.delivery_location = new_data.deliveryLocation
        if new_data.deliveryTimeline:
            existing.delivery_timeline = new_data.deliveryTimeline
        if new_data.buyerContactName:
            existing.buyer_contact_name = new_data.buyerContactName
        if new_data.buyerContactEmail:
            existing.buyer_contact_email = new_data.buyerContactEmail
        if new_data.buyerContactPhone:
            existing.buyer_contact_phone = new_data.buyerContactPhone
        if new_data.numberOfAmendments is not None:
            existing.number_of_amendments = new_data.numberOfAmendments
        if new_data.corrigendaLinks:
            existing.corrigenda_links = new_data.corrigendaLinks
        if new_data.nitDocumentLinks:
            existing.nit_document_links = new_data.nitDocumentLinks
        if new_data.amendmentLinks:
            existing.amendment_links = new_data.amendmentLinks
        # Also update description if richer data available
        if new_data.description and len(new_data.description) > len(existing.description or ""):
            existing.description = new_data.description

    # Detailed-card fields — fill only when empty so a richer re-scrape self-heals
    # old rows without clobbering manually-corrected values. Truncated to column
    # width (see _trunc) so enrichment can't overflow the VARCHAR limits either.
    if getattr(new_data, "location", None) and not existing.location:
        existing.location = _trunc(new_data.location, 255)
    if getattr(new_data, "bidType", None) and not existing.bid_type:
        existing.bid_type = _trunc(new_data.bidType, 50)
    if getattr(new_data, "sourcePortal", None) and not existing.source_portal:
        existing.source_portal = _trunc(new_data.sourcePortal, 50)
    if getattr(new_data, "category", None) and not existing.category:
        existing.category = _trunc(new_data.category, 255)

    existing.updated_at = datetime.now(timezone.utc)


def log_scrape_session(
    db: Session,
    user_id: int,
    status_input: ScrapeStatusInput,
) -> ScrapeLog:
    """Log a scrape session report from the extension."""
    log = ScrapeLog(
        user_id=user_id,
        portal=status_input.portal,
        session_id=status_input.sessionId,
        status=status_input.status,
        tenders_found=status_input.tendersFound,
        error_message=status_input.error,
    )

    if status_input.status in ("completed", "error"):
        log.completed_at = datetime.now(timezone.utc)

    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def _apply_tender_filters(query, *, portal=None, search=None, department=None, status=None, priority=None,
                          workflow_status=None, assigned_to=None, bid_type=None, location=None,
                          include_archived=False, include_below_threshold=False,
                          segment=None, score_min=None, score_max=None,
                          value_min=None, value_max=None, emd_min=None, emd_max=None,
                          closing_after=None, closing_before=None, eligibility_status=None,
                          unscored=False, exclude_expired=False):
    # An unscored row has no threshold to be below, so the below-threshold gate
    # must never hide it. Applied HERE rather than per-route so every caller
    # inherits it — view-counts missed this and its New pile undercounted the
    # very list it labels.
    if unscored:
        include_below_threshold = True
    # NULL-tolerant on purpose: `is_archived` is nullable with no server_default,
    # and `== False` drops NULL rows under SQL three-valued logic. A legacy NULL
    # means "not archived", and must count the same way everywhere or the
    # dashboard reports two different totals in one payload.
    if not include_archived:
        query = query.filter(Tender.is_archived.isnot(True))
    if exclude_expired:
        # View-only filter: hide tenders whose closing date has passed. NULL is
        # deliberately kept — a tender with no scraped closing date is UNKNOWN,
        # not expired, and `closing_date >= now` alone would drop those rows
        # under SQL three-valued logic. This never archives or deletes anything;
        # the archive sweep's past-due predicate is separate and much narrower
        # (see app/services/tender_archive_service.py).
        query = query.filter(or_(Tender.closing_date.is_(None),
                                 Tender.closing_date >= datetime.now(timezone.utc)))
    if not include_below_threshold:
        query = query.filter(Tender.below_threshold.isnot(True))   # NULL = not below
    if portal:
        query = query.filter(Tender.portal == portal)
    if search:
        # Require each supplied title keyword, regardless of its order in the title.
        for keyword in search.split():
            query = query.filter(Tender.title.ilike(f"%{keyword}%"))
    if department:
        query = query.filter(Tender.department.ilike(f"%{department}%"))
    if status:
        query = query.filter(Tender.status == status.lower())
    if priority:
        query = query.filter(Tender.priority == priority.lower())
    if workflow_status:
        query = query.filter(Tender.workflow_status == workflow_status)
    if assigned_to is not None:
        query = query.filter(Tender.assigned_to == assigned_to)
    if bid_type:
        query = query.filter(Tender.bid_type == bid_type)
    if location:
        query = query.filter(Tender.location.ilike(f"%{location}%"))
    if segment:
        query = query.filter(Tender.segment == segment)
    if score_min is not None:
        query = query.filter(Tender.ai_relevance_score >= score_min)
    if score_max is not None:
        query = query.filter(Tender.ai_relevance_score <= score_max)
    if unscored:
        query = query.filter(Tender.ai_relevance_score.is_(None))
    if value_min is not None:
        query = query.filter(Tender.estimated_value >= value_min)
    if value_max is not None:
        query = query.filter(Tender.estimated_value <= value_max)
    if emd_min is not None:
        query = query.filter(Tender.emd_amount >= emd_min)
    if emd_max is not None:
        query = query.filter(Tender.emd_amount <= emd_max)
    if closing_after is not None:
        query = query.filter(Tender.closing_date >= closing_after)
    if closing_before is not None:
        query = query.filter(Tender.closing_date <= closing_before)
    if eligibility_status:
        query = query.filter(Tender.eligibility_status == eligibility_status)
    return query


def count_tenders(db, **filters) -> int:
    from sqlalchemy import func
    # count uses the same filters; drop sort/limit/offset kwargs if present
    for k in ("sort_by", "limit", "offset"):
        filters.pop(k, None)
    q = _apply_tender_filters(db.query(Tender), **filters)
    return q.with_entities(func.count(Tender.id)).scalar() or 0


def get_tenders(
    db: Session,
    portal: Optional[str] = None,
    search: Optional[str] = None,
    department: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    workflow_status: Optional[str] = None,
    assigned_to: Optional[int] = None,
    sort_by: str = "created_at",
    bid_type: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    include_archived: bool = False,
    include_below_threshold: bool = False,
    segment: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
    value_min: Optional[float] = None,
    value_max: Optional[float] = None,
    emd_min: Optional[float] = None,
    emd_max: Optional[float] = None,
    closing_after=None,
    closing_before=None,
    eligibility_status: Optional[str] = None,
    unscored: bool = False,
    exclude_expired: bool = False,
) -> list[Tender]:
    """Query tenders with optional filters and sorting."""
    query = _apply_tender_filters(
        db.query(Tender),
        portal=portal, search=search, department=department, status=status, priority=priority,
        workflow_status=workflow_status, assigned_to=assigned_to, bid_type=bid_type,
        location=location, include_archived=include_archived,
        include_below_threshold=include_below_threshold,
        segment=segment, score_min=score_min, score_max=score_max,
        value_min=value_min, value_max=value_max, emd_min=emd_min, emd_max=emd_max,
        closing_after=closing_after, closing_before=closing_before,
        eligibility_status=eligibility_status, unscored=unscored,
        exclude_expired=exclude_expired,
    )

    # Sorting
    if sort_by == "relevance":
        # Most rows share a NULL ai_relevance_score, so ordering purely on it
        # is nondeterministic within that group and breaks offset/limit
        # pagination. Add a stable secondary key.
        query = query.order_by(Tender.ai_relevance_score.desc().nullslast(), Tender.created_at.desc())
    else:
        sort_map = {
            "created_at": Tender.created_at.desc(),
            "closing_date": Tender.closing_date.asc().nullslast(),
            "submission_deadline": Tender.submission_deadline.asc().nullslast(),
            "priority": Tender.priority.asc(),
        }
        order = sort_map.get(sort_by, Tender.created_at.desc())
        query = query.order_by(order)

    return query.offset(offset).limit(limit).all()


def update_tender(
    db: Session,
    tender_id: int,
    priority: Optional[str] = None,
    workflow_status: Optional[str] = None,
    submission_deadline: Optional[datetime] = None,
) -> Optional[Tender]:
    """Update tender organization fields."""
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        return None

    valid_priorities = {"critical", "high", "medium", "low"}
    valid_statuses = {"new", "in_progress", "checklist_ready", "proposal_draft", "proposal_review", "approved", "submitted"}

    if priority and priority.lower() in valid_priorities:
        tender.priority = priority.lower()
    if workflow_status and workflow_status in valid_statuses:
        tender.workflow_status = workflow_status
    if submission_deadline is not None:
        tender.submission_deadline = submission_deadline

    tender.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(tender)
    return tender


def assign_tender(
    db: Session,
    tender_id: int,
    user_id: int,
) -> Optional[Tender]:
    """Assign a tender to a user."""
    from app.models.user import User
    from app.services import notification_service

    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        return None

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None

    previously_assigned_to = tender.assigned_to
    tender.assigned_to = user_id
    tender.assigned_at = datetime.now(timezone.utc)
    if tender.workflow_status == "new":
        tender.workflow_status = "in_progress"
    tender.updated_at = datetime.now(timezone.utc)

    # Notify the user about the new assignment (only if it actually changed).
    if previously_assigned_to != user_id:
        try:
            notification_service.create(
                db,
                user_id=user_id,
                kind="tender.assigned",
                title=f"Tender assigned to you — {tender.title or tender.tender_id}",
                body=(
                    f"You have been assigned tender "
                    f"{tender.portal.upper()}-{tender.tender_id}.\n"
                    f"Closing date: "
                    f"{tender.closing_date.strftime('%Y-%m-%d %H:%M UTC') if tender.closing_date else 'not set'}."
                ),
                action_url=f"/tenders/{tender.id}/command-center",
                tender_id=tender.id,
            )
        except Exception:  # noqa: BLE001 — never fail the assignment on notification error
            import logging
            logging.getLogger(__name__).warning(
                "tender_service.assign_tender: notification dispatch failed", exc_info=True,
            )

    db.commit()
    db.refresh(tender)
    return tender


def get_users_list(db: Session) -> list:
    """Get list of active users for assignment dropdown."""
    from app.models.user import User
    return db.query(User).filter(User.is_active == True).all()


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Safely parse various date string formats."""
    if not date_str:
        return None

    date_str = date_str.strip()

    # Try ISO format first
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass

    # Try common date formats from aggregator sites
    formats = [
        "%d/%m/%Y %H:%M",    # "27/05/2026 15:17" (IREPS Due Date/Time)
        "%d/%m/%Y %H:%M:%S", # "27/05/2026 15:17:00"
        "%d-%m-%Y %H:%M",    # "27-05-2026 15:17"
        "%d-%m-%Y %H:%M:%S", # "27-05-2026 15:17:00"
        "%d %B %Y",       # "25 March 2026"
        "%d %b %Y",       # "25 Mar 2026"
        "%B %d, %Y",      # "March 25, 2026"
        "%b %d, %Y",      # "Mar 25, 2026"
        "%d-%m-%Y",       # "25-03-2026"
        "%d/%m/%Y",       # "25/03/2026"
        "%Y-%m-%d",       # "2026-03-25"
        "%d %b, %Y",      # "25 Mar, 2026"
        "%d-%b-%Y",       # "25-Mar-2026"
        "%b %d %Y",       # "Mar 25 2026"
        "%d %B, %Y",      # "25 March, 2026"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None
