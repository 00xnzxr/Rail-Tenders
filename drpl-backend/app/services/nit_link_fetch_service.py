"""Guarded backend fetch of a tender's public document links.

Turns a stored public URL (nit_document_links / document_links) into a
TenderDocument. Fallback to the extension download path — only actually-public
links succeed; gated links fail the content checks and are skipped. See design
docs/superpowers/specs/2026-07-21-backend-nit-link-fetch-design.md.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.parse import urlparse as _urlparse

from app.services.storage_service import get_storage_service, key_tender_doc

logger = logging.getLogger(__name__)

# Source of truth: the extension's host_permissions portal set.
PORTAL_HOST_ALLOWLIST: tuple[str, ...] = (
    "ireps.gov.in",
    "gem.gov.in",
    "mkp.gem.gov.in",
    "tendertiger.com",
    "bidassist.com",
    "tenderdetail.com",
    "tendersinfo.com",
    "projectstoday.com",
)

_DOC_CONTENT_TYPES = ("application/pdf",)
_PDF_MAGIC = b"%PDF-"

_EXTRA_UNSAFE_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),   # RFC 6598 CGNAT / shared address space
)


def is_allowed_host(url: str) -> bool:
    """https URL whose host is (or is a subdomain of) an allowlisted portal."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    for dom in PORTAL_HOST_ALLOWLIST:
        if host == dom or host.endswith("." + dom):
            return True
    return False


def resolves_to_private_ip(host: str) -> bool:
    """True if the host resolves to any private/loopback/link-local/reserved IP.

    Fails CLOSED: a resolution error returns True (treat as unsafe → skip).
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped   # evaluate the embedded IPv4's flags instead
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                or any(ip.version == net.version and ip in net for net in _EXTRA_UNSAFE_NETS)):
            return True
    return False


def is_document_response(status: int, content_type: str, first_bytes: bytes,
                         size: int, *, max_bytes: int) -> bool:
    """200 + document content-type + real magic bytes + within size cap."""
    if status != 200:
        return False
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in _DOC_CONTENT_TYPES:
        return False
    if size > max_bytes:
        return False
    if not first_bytes.startswith(_PDF_MAGIC):
        return False
    return True


@dataclass
class FetchResult:
    fetched: int = 0
    skipped_not_public: int = 0
    skipped_dedup: int = 0
    failed: int = 0
    doc_ids: list = field(default_factory=list)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects — the SSRF guard only validated the original
    host/IP, so a redirect target must never be fetched. We surface the 3xx
    status to the caller, which treats non-200 as 'not a document'."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # do not follow


def _http_get(url: str, *, timeout: int, max_bytes: int):
    """GET a URL with a browser UA, capped body read, NO redirect following.
    Returns (status, content_type, body_bytes) or None on network error.
    A 3xx is returned as-is (status != 200) so the caller's document check
    rejects it — we never fetch a redirect target the guards didn't validate.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (drpl-backend)"})
    try:
        with opener.open(req, timeout=timeout) as r:
            status = r.status
            ct = r.headers.get("content-type", "")
            body = r.read(max_bytes + 1)
            return status, ct, body
    except urllib.error.HTTPError as e:
        # A 3xx with redirects disabled raises HTTPError; surface its code so
        # the caller sees a non-200 and skips it. Read a little body for ctype.
        try:
            body = e.read(max_bytes + 1)
        except Exception:
            body = b""
        return e.code, e.headers.get("content-type", "") if e.headers else "", body
    except Exception as e:
        logger.info(f"[nit_fetch] GET failed for {url}: {type(e).__name__}: {e}")
        return None


def _filename_from_url(url: str, tender_id: int) -> str:
    import re
    try:
        base = os.path.basename(_urlparse(url).path) or ""
    except Exception:
        base = ""
    if not re.search(r"\.[a-z0-9]{1,8}$", base, re.IGNORECASE):
        base = f"nit_{tender_id}.pdf"
    return base


def fetch_document_links_for_tender(db, tender_id, *, links=None,
                                    mark_nit=True, background_tasks=None) -> FetchResult:
    """Fetch a tender's public document links into TenderDocument rows.

    Never raises. Honors the (tender, source_url) dedup, the host allowlist,
    the private-IP guard, and the content/size checks. Returns a FetchResult.
    """
    from app.core.config import get_settings
    from app.models.tender import Tender, TenderDocument

    res = FetchResult()
    settings = get_settings()
    try:
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if not tender:
            return res

        # Candidate URLs: nit links first (marked nit), then document links.
        nit_links = list(getattr(tender, "nit_document_links", None) or [])
        doc_links = list(getattr(tender, "document_links", None) or [])
        if links is not None:
            candidates = [(u, mark_nit) for u in links]
        else:
            candidates = [(u, True) for u in nit_links] + [(u, False) for u in doc_links]

        cap = settings.nit_link_fetch_max_docs_per_tender
        candidates = candidates[:cap]
        seen_urls: set = set()
        for url, is_nit in candidates:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            if not is_allowed_host(url):
                res.skipped_not_public += 1
                continue

            # Dedup: already have this (tender, source_url)?
            dup = (
                db.query(TenderDocument)
                .filter(TenderDocument.tender_id == tender_id,
                        TenderDocument.source_url == url)
                .first()
            )
            if dup:
                res.skipped_dedup += 1
                continue

            host = _urlparse(url).hostname or ""
            if resolves_to_private_ip(host):
                res.skipped_not_public += 1
                continue

            got = _http_get(url, timeout=settings.nit_link_fetch_timeout_s,
                            max_bytes=settings.nit_link_fetch_max_bytes)
            time.sleep(settings.nit_link_fetch_delay_s)
            if not got:
                res.failed += 1
                continue
            status, ctype, body = got
            if not is_document_response(status, ctype, body[:8], len(body),
                                        max_bytes=settings.nit_link_fetch_max_bytes):
                res.skipped_not_public += 1
                continue

            file_name = _filename_from_url(url, tender_id)
            key = key_tender_doc(tender_id, file_name)
            try:
                get_storage_service().upload_file_sync(
                    key, body, content_type="application/pdf")
                doc = TenderDocument(
                    tender_id=tender_id,
                    file_name=file_name,
                    file_path=key,
                    file_size=len(body),
                    mime_type="application/pdf",
                    document_type="nit" if is_nit else "other",
                    source_url=url,
                    uploaded_by=None,
                )
                db.add(doc)
                db.commit()
                db.refresh(doc)
                res.fetched += 1
                res.doc_ids.append(doc.id)
                logger.info(f"[nit_fetch] tender {tender_id}: stored {file_name} "
                            f"from {url} (nit={is_nit})")
            except Exception as e:
                logger.warning(f"[nit_fetch] tender {tender_id}: persist failed for "
                               f"{url}: {type(e).__name__}: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass
                res.failed += 1
    except Exception as e:
        logger.warning(f"[nit_fetch] tender {tender_id}: fatal, returning partial "
                       f"result: {type(e).__name__}: {e}")
    return res


def fetch_links_for_tenders_job(tender_ids):
    """RQ entrypoint: fetch public links for a list of tenders. Opens its own
    DB session (safe in a worker). Never raises out of the job."""
    from app.core.database import SessionLocal
    db = SessionLocal()
    try:
        for tid in tender_ids:
            try:
                fetch_document_links_for_tender(db, tid)
            except Exception:
                logger.warning(f"[nit_fetch] job failed for tender {tid}", exc_info=True)
    finally:
        db.close()
