"""
DRPL Backend - Notification email templates.

Plain-string renderer for the small set of notification emails. No Jinja2 dep:
each notification's body is short and the layout is uniform — header strip,
title, body, primary CTA link, footer.

`render_notification_html(notif, user)` is the only public entry point. It uses
``notif.title``, ``notif.body``, and ``notif.action_url`` directly; per-kind
customisation happens in the dispatch sites that populate those fields, not here.
"""

from __future__ import annotations

import html as _html_lib
import logging

from app.core.config import get_settings
from app.models.notification import Notification
from app.models.user import User


_log = logging.getLogger(__name__)


def _resolve_base_url() -> str:
    """DB-first resolution of the app base URL with env fallback."""
    env = get_settings()
    try:
        from app.core.database import SessionLocal
        from app.services.settings_service import get_setting_value
        db = SessionLocal()
        try:
            val = get_setting_value(db, "notifications_app_base_url")
            if val and str(val).strip():
                return str(val).rstrip("/")
        finally:
            db.close()
    except Exception as e:
        _log.debug("notification_templates: base url lookup failed: %s", e)
    return (env.notifications_app_base_url or "").rstrip("/")


_BASE_STYLE = """
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  color: #1f2937;
  line-height: 1.6;
"""

_BUTTON_STYLE = (
    "display:inline-block;background:#1e3a8a;color:#ffffff;"
    "padding:10px 20px;border-radius:6px;text-decoration:none;"
    "font-weight:600;font-size:14px;"
)


def render_notification_html(notif: Notification, user: User) -> str:
    """Render a notification into a single-purpose HTML email."""
    base_url = _resolve_base_url()

    cta_html = ""
    if notif.action_url:
        href = notif.action_url
        if href.startswith("/") and base_url:
            href = f"{base_url}{href}"
        cta_html = (
            f'<p style="margin:24px 0;">'
            f'<a href="{_html_lib.escape(href, quote=True)}" style="{_BUTTON_STYLE}">'
            f"Open in DRPL"
            f"</a></p>"
        )

    body_html = ""
    if notif.body:
        body_paragraphs = "".join(
            f"<p>{_html_lib.escape(line)}</p>"
            for line in notif.body.splitlines()
            if line.strip()
        )
        body_html = body_paragraphs

    greeting = _html_lib.escape((user.name or user.email or "there").split()[0])

    return f"""<!DOCTYPE html>
<html><body style="{_BASE_STYLE} margin:0;padding:24px;background:#f8fafc;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
         style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:8px;
                border:1px solid #e5e7eb;padding:32px;">
    <tr><td>
      <p style="margin:0 0 16px 0;color:#6b7280;font-size:13px;
                text-transform:uppercase;letter-spacing:0.05em;font-weight:600;">
        DRPL Platform
      </p>
      <h1 style="margin:0 0 16px 0;font-size:20px;font-weight:600;color:#111827;">
        {_html_lib.escape(notif.title)}
      </h1>
      <p style="margin:0 0 16px 0;">Hi {greeting},</p>
      {body_html}
      {cta_html}
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
      <p style="margin:0;font-size:12px;color:#9ca3af;">
        You're receiving this because you have an account on DRPL Platform.
        Notification settings are managed by your administrator.
      </p>
    </td></tr>
  </table>
</body></html>
"""


def render_digest_html(user: User, items: list[Notification]) -> str:
    """Render a daily-digest email — one section per notification."""
    base_url = _resolve_base_url()
    greeting = _html_lib.escape((user.name or user.email or "there").split()[0])

    rows = []
    for it in items:
        href = it.action_url or ""
        if href.startswith("/") and base_url:
            href = f"{base_url}{href}"
        link_html = ""
        if href:
            link_html = (
                f' <a href="{_html_lib.escape(href, quote=True)}" '
                f'style="color:#1e3a8a;text-decoration:none;">View &rarr;</a>'
            )
        rows.append(
            f'<li style="margin:8px 0;">'
            f"<strong>{_html_lib.escape(it.title)}</strong>"
            f"{link_html}"
            f"</li>"
        )
    items_html = "<ul style=\"padding-left:20px;\">" + "".join(rows) + "</ul>"

    return f"""<!DOCTYPE html>
<html><body style="{_BASE_STYLE} margin:0;padding:24px;background:#f8fafc;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
         style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:8px;
                border:1px solid #e5e7eb;padding:32px;">
    <tr><td>
      <p style="margin:0 0 16px 0;color:#6b7280;font-size:13px;
                text-transform:uppercase;letter-spacing:0.05em;font-weight:600;">
        DRPL Daily Digest
      </p>
      <h1 style="margin:0 0 16px 0;font-size:20px;font-weight:600;color:#111827;">
        {len(items)} update{'s' if len(items) != 1 else ''} on your tenders
      </h1>
      <p style="margin:0 0 16px 0;">Hi {greeting}, here's what changed in the
        last 24 hours:</p>
      {items_html}
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
      <p style="margin:0;font-size:12px;color:#9ca3af;">
        You're receiving this because you have an account on DRPL Platform.
        Daily digest schedule is configured by your administrator.
      </p>
    </td></tr>
  </table>
</body></html>
"""
