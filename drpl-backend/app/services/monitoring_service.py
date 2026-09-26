"""
DRPL Backend - Portal Health Monitoring Service
Computes per-portal health metrics from scrape logs and tender data
"""

from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.tender import Tender, ScrapeLog


PORTALS = ["ireps", "gem", "tendertiger", "bidassist"]


def get_portal_health(db: Session) -> list[dict]:
    """Compute health metrics for each portal."""
    now = datetime.now(timezone.utc)
    results = []

    for portal in PORTALS:
        # Last successful scrape
        last_success = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.status == "completed",
        ).order_by(ScrapeLog.started_at.desc()).first()

        # Tender counts
        tenders_24h = db.query(Tender).filter(
            Tender.portal == portal,
            Tender.created_at >= now - timedelta(hours=24),
        ).count()

        tenders_7d = db.query(Tender).filter(
            Tender.portal == portal,
            Tender.created_at >= now - timedelta(days=7),
        ).count()

        tenders_30d = db.query(Tender).filter(
            Tender.portal == portal,
            Tender.created_at >= now - timedelta(days=30),
        ).count()

        # Success rate (last 7 days)
        total_sessions = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.started_at >= now - timedelta(days=7),
        ).count()

        successful_sessions = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.status == "completed",
            ScrapeLog.started_at >= now - timedelta(days=7),
        ).count()

        success_rate = (successful_sessions / total_sessions) if total_sessions > 0 else 1.0

        # Last error
        last_error_log = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.status == "error",
        ).order_by(ScrapeLog.started_at.desc()).first()

        # Determine health status
        if last_success is None:
            # No scrapes ever — show as warning (not error, since it may just be new)
            health_status = "warning"
        else:
            hours_since_last = (now - last_success.started_at.replace(tzinfo=timezone.utc if last_success.started_at.tzinfo is None else last_success.started_at.tzinfo)).total_seconds() / 3600

            if hours_since_last <= 24 and success_rate >= 0.8:
                health_status = "healthy"
            elif hours_since_last <= 48 or success_rate >= 0.5:
                health_status = "warning"
            else:
                health_status = "error"

        results.append({
            "portal": portal,
            "status": health_status,
            "last_successful_scrape": last_success.started_at if last_success else None,
            "tenders_24h": tenders_24h,
            "tenders_7d": tenders_7d,
            "tenders_30d": tenders_30d,
            "success_rate": round(success_rate, 2),
            "last_error": last_error_log.error_message if last_error_log else None,
            "selectors_version": "1.0.0",
        })

    return results


def get_portal_alerts(db: Session) -> list[dict]:
    """Detect anomalies and generate alerts."""
    now = datetime.now(timezone.utc)
    alerts = []

    for portal in PORTALS:
        # Check: Portal going dark (no activity in >24h)
        recent_activity = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.started_at >= now - timedelta(hours=24),
        ).count()

        # Only alert if portal has ever had activity
        has_ever_had_activity = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
        ).count() > 0

        if recent_activity == 0 and has_ever_had_activity:
            alerts.append({
                "portal": portal,
                "alert_type": "portal_dark",
                "message": f"No scraping activity on {portal.upper()} in the last 24 hours",
                "severity": "warning",
                "detected_at": now,
            })

        # Check: Selector breakage (3+ consecutive sessions with 0 tenders)
        recent_sessions = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.status == "completed",
        ).order_by(ScrapeLog.started_at.desc()).limit(3).all()

        if len(recent_sessions) >= 3 and all(s.tenders_found == 0 for s in recent_sessions):
            alerts.append({
                "portal": portal,
                "alert_type": "selector_breakage",
                "message": f"Last 3 scrape sessions on {portal.upper()} found 0 tenders — selectors may be broken",
                "severity": "critical",
                "detected_at": now,
            })

        # Check: Error spike (>50% error rate in last 6 hours)
        last_6h = now - timedelta(hours=6)
        sessions_6h = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.started_at >= last_6h,
        ).count()

        errors_6h = db.query(ScrapeLog).filter(
            ScrapeLog.portal == portal,
            ScrapeLog.status == "error",
            ScrapeLog.started_at >= last_6h,
        ).count()

        if sessions_6h >= 2 and (errors_6h / sessions_6h) > 0.5:
            alerts.append({
                "portal": portal,
                "alert_type": "error_spike",
                "message": f"High error rate on {portal.upper()}: {errors_6h}/{sessions_6h} sessions failed in last 6 hours",
                "severity": "critical",
                "detected_at": now,
            })

    return alerts
