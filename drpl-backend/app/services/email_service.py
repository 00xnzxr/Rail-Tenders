"""
DRPL Backend - Email service (Resend).

Thin wrapper around the Resend HTTP API. One outbound POST per email.

When ``settings.resend_api_key`` is empty (local dev), `send_email()` returns
`{"skipped": "not_configured"}` and logs a one-liner. The caller doesn't need
to branch — the rest of the notification pipeline still records the in-app
row and stamps `email_sent_at` as a side-effect (we leave it `None` here so a
later retry is possible if the key is added).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from app.core.config import get_settings


log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _resolve(key: str, env_default: str) -> str:
    """DB-first / env-fallback resolution for a single setting.

    Email-send paths run inside RQ workers, so we open a short-lived DB session
    rather than depending on a FastAPI request scope. Any DB error degrades
    cleanly to the env default — we never want notification dispatch to fail
    because the settings table is briefly unreachable.
    """
    try:
        from app.core.database import SessionLocal
        from app.services.settings_service import get_setting_value
        db = SessionLocal()
        try:
            val = get_setting_value(db, key)
            if val is not None and str(val).strip() != "":
                return str(val)
        finally:
            db.close()
    except Exception as e:
        log.debug("email_service: setting lookup failed for %s: %s", key, e)
    return (env_default or "").strip()


def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
) -> dict:
    """Send a single transactional email via Resend.

    Returns the Resend response JSON on success, or a `{"skipped": ...}` dict
    when the send was bypassed (no API key, no recipient, etc.). Raises
    :class:`httpx.HTTPStatusError` on non-2xx responses so the caller (RQ job)
    sees the failure and can retry.
    """
    env = get_settings()
    api_key = _resolve("resend_api_key", env.resend_api_key)
    from_email = _resolve("resend_from_email", env.resend_from_email)

    if not api_key:
        log.warning("email_service: RESEND_API_KEY empty — dropping email to %s", to)
        return {"skipped": "not_configured"}
    if not from_email:
        log.warning("email_service: resend_from_email empty — dropping email to %s", to)
        return {"skipped": "no_from_address"}
    if not to:
        return {"skipped": "no_recipient"}

    payload = {
        "from": from_email,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text or _html_to_text(html),
    }

    try:
        resp = httpx.post(
            RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.error(
            "email_service: Resend returned %s for %s — %s",
            e.response.status_code, to, e.response.text[:300],
        )
        raise
    except httpx.HTTPError as e:
        log.error("email_service: HTTP error sending to %s — %s", to, e)
        raise

    body = resp.json() if resp.content else {}
    log.info("email_service: sent email id=%s to=%s subject=%r",
             body.get("id"), to, subject)
    return body


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    """Crude HTML → plain-text for the email `text` fallback.

    Resend accepts either or both; supplying a plain-text version improves
    deliverability and gives non-HTML clients something to render. We don't
    need fidelity — just strip tags and collapse whitespace.
    """
    no_tags = _TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", no_tags).strip()
