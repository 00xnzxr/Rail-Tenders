"""
DRPL Collector - the sink.

Shared contract 3, and the only one that is an HTTP call: ``POST
/api/extension/tenders`` with the exact body the Chrome extension has always
sent. Everything downstream of that -- dedupe on (portal, tender_id), relevance
scoring, the eager NIT fetch -- already exists and fans out on its own. The
collector's job ends when the POST returns.

Why reuse the extension endpoint rather than add a collector-specific one:
it is already the tested ingest path, it already returns ``new_ids`` for the
scoring fan-out, and reusing it means the extension and the collector cannot
drift into two different definitions of "a tender".

Auth is a service account. Prefer a ``drpl_...`` API token over a JWT: it does
not expire, it is attributable, and revoking it locks out the collector without
locking out a person.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from collector.config import get_settings

logger = logging.getLogger(__name__)


class SinkError(Exception):
    """The batch did not land. The ledger must NOT be written."""


@dataclass
class SinkResult:
    """What drpl-backend's ``BatchUploadResponse`` came back with."""

    received: int = 0
    new: int = 0
    duplicates: int = 0
    errors: int = 0
    new_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_response(cls, data: dict) -> "SinkResult":
        return cls(
            received=int(data.get("received") or 0),
            new=int(data.get("new") or 0),
            duplicates=int(data.get("duplicates") or 0),
            errors=int(data.get("errors") or 0),
            new_ids=list(data.get("new_ids") or []),
        )


#: Status codes worth another go. 401/403 are not: a bad token will still be
#: bad in four seconds, and retrying it just burns the sweep's time budget.
_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class Sink:
    """A client for the ingest endpoint, with bounded retry and backoff."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            s = get_settings()
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(s.sink_timeout_seconds, connect=20.0),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def post(self, tenders: list[dict]) -> SinkResult:
        """Ship one batch. Raises SinkError when it did not land.

        The caller writes the ledger only after this returns, so raising here
        is what makes a failed upload retryable on the next sweep instead of a
        tender lost for good.
        """
        if not tenders:
            return SinkResult()

        s = get_settings()
        if not s.drpl_service_token:
            raise SinkError(
                "DRPL_SERVICE_TOKEN is not set -- the collector has no way to "
                "authenticate to /api/extension/tenders."
            )

        client = await self._get_client()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {s.drpl_service_token}",
        }
        body = {"tenders": tenders}
        last_error = "unknown"

        for attempt in range(1, s.sink_max_retries + 1):
            try:
                r = await client.post(s.ingest_url, json=body, headers=headers)
            except httpx.HTTPError as e:
                last_error = f"network: {e}"
                if attempt >= s.sink_max_retries:
                    break
                await asyncio.sleep(2.0 * attempt)
                continue

            if r.status_code == 200:
                try:
                    return SinkResult.from_response(r.json())
                except ValueError as e:
                    raise SinkError(f"ingest returned non-JSON: {e}") from e

            if r.status_code in (401, 403):
                raise SinkError(
                    f"ingest rejected the service token ({r.status_code}). "
                    "Check DRPL_SERVICE_TOKEN."
                )

            last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code not in _RETRYABLE or attempt >= s.sink_max_retries:
                break
            # Honour Retry-After when the server states one; otherwise back off.
            delay = 2.0 * attempt
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            await asyncio.sleep(delay)

        raise SinkError(f"ingest failed after {s.sink_max_retries} attempts -- {last_error}")


class InProcessSink(Sink):
    """The ingest path called directly, as the user who pressed Search.

    Inside drpl-backend there is no HTTP hop and no service token: the batch
    goes through ``tender_service.ingest_tender_batch`` -- the same function
    ``POST /api/extension/tenders`` calls -- on a session of its own, and the
    scoring and NIT-fetch fan-outs are dispatched the same way the route does.
    The contract (the ``TenderInput`` shape, the ``new_ids`` the events carry)
    is unchanged; only the transport is gone.
    """

    def __init__(self, user_id: int) -> None:
        super().__init__(client=None)
        self.user_id = int(user_id)

    async def post(self, tenders: list[dict]) -> SinkResult:
        if not tenders:
            return SinkResult()
        return await asyncio.to_thread(self._ingest, tenders)

    def _ingest(self, tenders: list[dict]) -> SinkResult:
        from pydantic import ValidationError

        from app.core.database import SessionLocal
        from app.schemas import TenderInput
        from app.services.tender_service import ingest_tender_batch

        rows: list = []
        bad = 0
        for t in tenders:
            try:
                rows.append(TenderInput(**t))
            except ValidationError as e:
                bad += 1
                logger.warning("sink: tender %s failed validation: %s",
                               t.get("tenderId"), str(e)[:200])
        db = SessionLocal()
        try:
            result = ingest_tender_batch(db, rows, self.user_id)
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            raise SinkError(f"ingest failed: {type(e).__name__}: {e}") from e
        finally:
            db.close()
        new_ids = list(getattr(result, "new_ids", None) or [])
        if new_ids:
            try:
                from app.api.routes.extension import _dispatch_nit_fetch, _dispatch_scoring
                _dispatch_scoring(new_ids, None)
                _dispatch_nit_fetch(new_ids, None)
            except Exception as e:  # noqa: BLE001 -- a fan-out miss never loses a tender
                logger.warning("sink: post-ingest dispatch failed: %s", e)
        return SinkResult(
            received=int(getattr(result, "received", len(rows)) or 0) + bad,
            new=int(getattr(result, "new", 0) or 0),
            duplicates=int(getattr(result, "duplicates", 0) or 0),
            errors=int(getattr(result, "errors", 0) or 0) + bad,
            new_ids=new_ids,
        )

    async def aclose(self) -> None:
        return None


async def post(tenders: list[dict]) -> SinkResult:
    """One-shot convenience for scripts. Opens and closes its own client."""
    sink = Sink()
    try:
        return await sink.post(tenders)
    finally:
        await sink.aclose()
