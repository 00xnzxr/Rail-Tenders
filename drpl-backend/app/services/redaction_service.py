"""
DRPL Backend - Redaction Service
Redacts sensitive information before sending to AI models.
Loads rules from database with cache, falls back to hardcoded patterns.
"""

import re
import time
import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Hardcoded fallback patterns (used when DB is unavailable)
FALLBACK_PATTERNS = [
    (r'\b[A-Z]{5}[0-9]{4}[A-Z]\b', '[REDACTED-PAN]'),
    (r'\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b', '[REDACTED-AADHAAR]'),
    (r'\b\d{9,18}\b(?=.*(?:account|a/c|bank))', '[REDACTED-BANK-ACCOUNT]'),
    (r'\b(?:\+91[\-\s]?)?[6-9]\d{9}\b', '[REDACTED-PHONE]'),
    (r'\b[A-Z]{4}0[A-Z0-9]{6}\b', '[REDACTED-IFSC]'),
    (r'\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z0-9]{2}\b', '[REDACTED-GST]'),
]

# In-memory cache for DB rules
_rules_cache: list[tuple[str, str]] = []
_cache_timestamp: float = 0
_CACHE_TTL = 60  # seconds


def _load_rules_from_db(db: Session) -> list[tuple[str, str]]:
    """Load enabled redaction rules from database."""
    global _rules_cache, _cache_timestamp

    now = time.time()
    if _rules_cache and (now - _cache_timestamp) < _CACHE_TTL:
        return _rules_cache

    try:
        from app.models.redaction_rule import RedactionRule
        rules = db.query(RedactionRule).filter(RedactionRule.is_enabled == True).all()
        if rules:
            _rules_cache = [(r.pattern, r.replacement) for r in rules]
            _cache_timestamp = now
            return _rules_cache
    except Exception as e:
        logger.warning(f"Failed to load redaction rules from DB: {e}")

    return FALLBACK_PATTERNS


def redact_text(text: str, db: Optional[Session] = None) -> str:
    """Redact sensitive information from text using DB rules or fallback patterns."""
    patterns = _load_rules_from_db(db) if db else FALLBACK_PATTERNS
    result = text
    for pattern, replacement in patterns:
        try:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        except re.error:
            continue
    return result


def redact_tender_context(text: str, db: Optional[Session] = None) -> str:
    """Redact sensitive info from tender context before AI processing."""
    return redact_text(text, db)


def redact_costing_query(
    text: str,
    anonymization_map: dict,
    db: Optional[Session] = None,
) -> str:
    """
    Redact a web search query before it leaves the system during costing research.

    Two-pass approach:
      Pass 1 — regex patterns (PAN, GST, IFSC, phone, Aadhaar) via redact_text()
      Pass 2 — entity substitution using the anonymization_map built at agent startup
                (company names, tender refs, project codes → placeholder tokens)

    Args:
        text: The raw search query string
        anonymization_map: Mapping of placeholder → original (e.g. {"[ENTITY-1]": "DRPL"}).
                           Keys are placeholders; values are original strings to replace.
        db: Optional DB session for loading custom redaction rules

    Returns:
        Sanitized query string safe to send to external search providers
    """
    # Pass 1: regex-based PII redaction
    result = redact_text(text, db)

    # Pass 2: entity substitution — replace each original string with its placeholder
    for placeholder, original in anonymization_map.items():
        if original and len(original) > 2:
            try:
                result = re.sub(re.escape(original), placeholder, result, flags=re.IGNORECASE)
            except re.error:
                continue

    return result
