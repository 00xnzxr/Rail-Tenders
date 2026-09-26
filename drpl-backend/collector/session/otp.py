"""
DRPL Collector - OTP registry.

The automation blocks on a pending code and something external delivers it,
signed. A spare handset with the dedicated SIM and an SMS-forwarding app POSTs
to ``/internal/otp``; the future here is what the login flow is awaiting.

SCOPE LINE, stated once so it is never ambiguous: the OTP is DRPL's own
credential, on DRPL's own registered IREPS account, delivered to DRPL's own
SIM. Automating its delivery is session management -- the same thing a password
manager does. A CAPTCHA is a different thing: it exists specifically to keep
automation out, and building a solver puts both the account and the client
relationship at risk. Where a CAPTCHA blocks a path, take the official route or
leave a human in the loop. ``captcha_detected`` below is the hook for exactly
that, and it pauses rather than solves.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class OtpTimeout(Exception):
    """No code arrived in time. Notify a human; fail the sweep loudly.

    Deliberately not swallowed: a silent OTP timeout means IREPS quietly
    stopped being collected, which is the failure this whole service exists to
    make impossible.
    """


class CaptchaEncountered(Exception):
    """A human verification challenge. Pause and ask; never solve."""


#: portal -> the future the login flow is blocked on. One per portal, because
#: one session per portal is the whole design -- two concurrent logins would
#: race for one SIM and both would fail.
_pending: dict[str, asyncio.Future] = {}
_lock = asyncio.Lock()


async def await_code(portal: str, timeout: Optional[int] = None) -> str:
    """Block until a code is delivered for ``portal``, or raise OtpTimeout."""
    from collector.config import get_settings

    if timeout is None:
        timeout = get_settings().otp_wait_seconds

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    async with _lock:
        existing = _pending.get(portal)
        if existing is not None and not existing.done():
            existing.cancel()
        _pending[portal] = fut

    logger.info("otp: waiting up to %ss for a %s code", timeout, portal)
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError as e:
        raise OtpTimeout(
            f"No OTP arrived for {portal} within {timeout}s. Check the forwarding "
            f"handset and the /internal/otp webhook."
        ) from e
    finally:
        async with _lock:
            if _pending.get(portal) is fut:
                _pending.pop(portal, None)


def deliver(portal: str, code: str) -> bool:
    """Hand a code to whoever is waiting. False when nobody was.

    False is not an error -- a code arriving with no login in flight is an SMS
    the portal sent for another reason, or a retry. Log it and drop it.
    """
    fut = _pending.get(portal)
    if fut is None or fut.done():
        logger.info("otp: a %s code arrived with nobody waiting -- dropped", portal)
        return False
    try:
        fut.get_loop().call_soon_threadsafe(fut.set_result, code)
    except RuntimeError:
        # The loop is gone (worker shut down mid-wait). Nothing to deliver to.
        return False
    return True


def waiting_for() -> list[str]:
    """Portals currently blocked on a code. For the health endpoint."""
    return [p for p, f in _pending.items() if not f.done()]


# -- Webhook verification ------------------------------------------------


def verify_signature(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 over ``<timestamp>.<raw body>``, compared in constant time.

    Signing the timestamp alongside the body is what makes the replay guard
    meaningful: without it an attacker could keep a valid signature and simply
    re-send it with a fresh timestamp header.
    """
    if not secret or not signature or not timestamp:
        return False
    expected = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def timestamp_fresh(timestamp: str, max_skew: int) -> bool:
    """Reject a replayed request, and one from a badly-set clock."""
    try:
        ts = int(float(timestamp))
    except (TypeError, ValueError):
        return False
    return abs(time.time() - ts) <= max_skew


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    """The signature a sender should produce. Used by the tests and the docs."""
    return hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()


def reset_for_tests() -> None:
    _pending.clear()
