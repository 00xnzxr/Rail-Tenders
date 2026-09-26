"""
DRPL Collector - fetching the bid document.

On GeM one PDF carries everything the listing does not: the scope, the
eligibility conditions, the EMD, the delivery schedule and the buyer's contact.
Every number the tender card is missing lives in there.

This module only *gets* the bytes. Reading them is `enrich/ai.py`'s job.

Two rules it holds to:

  * a document is fetched at most once per tender per run, through the same
    warm session the listing came from (the PDF endpoints are behind the same
    WAF cookies)
  * a fetch that fails is not an error worth failing a sweep over. The tender
    still ships with its listing data and the link; the next run tries again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from collector.config import get_settings

logger = logging.getLogger(__name__)

#: A real GeM bid PDF is 100-200 KB. Anything far outside that is either not a
#: PDF or a bundle we should not be pulling into memory on a worker.
MAX_BYTES_DEFAULT = 12 * 1024 * 1024

_PDF_MAGIC = b"%PDF"


@dataclass
class FetchedDocument:
    url: str
    content: bytes
    media_type: str

    @property
    def is_pdf(self) -> bool:
        return self.content[:4] == _PDF_MAGIC

    @property
    def size(self) -> int:
        return len(self.content)


async def fetch_document(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: Optional[int] = None,
) -> Optional[FetchedDocument]:
    """Download one document. Returns None on any failure -- never raises.

    Streams so an unexpectedly huge response is abandoned rather than read
    into memory in full.
    """
    s = get_settings()
    limit = max_bytes or s.enrich_max_document_bytes or MAX_BYTES_DEFAULT

    try:
        async with client.stream(
            "GET", url, headers={"Referer": f"{s.gem_base_url}/all-bids"}
        ) as response:
            if response.status_code != 200:
                logger.debug("document %s -> HTTP %s", url, response.status_code)
                return None

            declared = response.headers.get("content-length")
            if declared and int(declared) > limit:
                logger.info(
                    "document %s is %s bytes, over the %s limit -- skipped",
                    url, declared, limit,
                )
                return None

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    logger.info("document %s exceeded %s bytes mid-stream -- skipped", url, limit)
                    return None
                chunks.append(chunk)

            media_type = (response.headers.get("content-type") or "").split(";")[0].strip()

    except httpx.HTTPError as e:
        logger.debug("document %s could not be fetched: %s", url, e)
        return None
    except Exception as e:  # noqa: BLE001 -- a document is never worth a sweep
        logger.debug("document %s failed unexpectedly: %s", url, e)
        return None

    doc = FetchedDocument(url=url, content=b"".join(chunks), media_type=media_type or "application/pdf")
    if not doc.is_pdf:
        # GeM occasionally answers a document URL with an HTML error page and a
        # 200. Checking the magic bytes is the only reliable tell.
        logger.info("document %s did not come back as a PDF (%s) -- skipped", url, doc.media_type)
        return None
    return doc
