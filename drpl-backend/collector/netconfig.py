"""
DRPL Collector - outbound network configuration for the portal fetchers.

Why this exists: the first deploy of GeM Search failed with
``ConnectError: All connection attempts failed`` from the Railway worker while
the same request succeeded from a laptop in India and from a non-cloud US
address. GeM's F5 edge drops TCP from cloud egress ranges; nothing in the
request can change that, only the address it comes from. So every GeM client
is built here, and here is where an outbound proxy is honoured:

* ``gem_proxy_url`` in Admin > Platform Settings (secret, no redeploy), else
* ``GEM_PROXY_URL`` in the environment.

``http://``, ``https://`` and ``socks5://`` URLs work (SOCKS needs the
``socksio`` package, which requirements.txt carries). Empty means direct.

The second job of this module is to say *why* a connect failed. httpx folds
every per-address error into one sentence, so ``diagnose_connect`` re-does
the resolution and the TCP handshake itself and reports the errno per
address -- a refused, a timeout and an unreachable each mean a different fix.
"""

from __future__ import annotations

import errno as _errno
import logging
import os
import socket
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SETTING_KEY = "gem_proxy_url"
ENV_KEY = "GEM_PROXY_URL"


def gem_proxy_url(db=None) -> Optional[str]:
    """The proxy GeM traffic goes through, or None for a direct connection.

    Admin setting first, then the collector's settings object (which reads
    the environment), then the bare environment variable -- the same order
    the RunPod key resolves in.
    """
    if db is not None:
        try:
            from app.services.settings_service import get_setting_value

            v = get_setting_value(db, SETTING_KEY, None)
            if v and str(v).strip():
                return str(v).strip()
        except Exception:  # noqa: BLE001 -- a settings miss never blocks a sweep
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    else:
        v = _proxy_from_platform_settings()
        if v:
            return v
    try:
        from collector.config import get_settings

        v = (get_settings().gem_proxy_url or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    v = (os.environ.get(ENV_KEY) or "").strip()
    return v or None


def _proxy_from_platform_settings() -> Optional[str]:
    """Open a short session of our own; the fetchers have none to lend."""
    try:
        from app.core.database import SessionLocal
    except Exception:  # noqa: BLE001 -- collector running outside the backend
        return None
    db = SessionLocal()
    try:
        from app.services.settings_service import get_setting_value

        v = get_setting_value(db, SETTING_KEY, None)
        return str(v).strip() if v and str(v).strip() else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        db.close()


def make_client(*, timeout: httpx.Timeout, headers: Optional[dict] = None,
                proxy: Optional[str] = "unset") -> httpx.AsyncClient:
    """Every GeM-facing httpx client, built one way.

    ``proxy`` defaults to whatever ``gem_proxy_url`` resolves; pass ``None``
    to force a direct connection (tests do).
    """
    if proxy == "unset":
        proxy = gem_proxy_url()
    kwargs = dict(timeout=timeout, follow_redirects=True, headers=headers or {})
    if proxy:
        kwargs["proxy"] = proxy
        logger.info("collector: GeM traffic routed through a proxy (%s).", redact(proxy))
    return httpx.AsyncClient(**kwargs)


def redact(url: str) -> str:
    """Host and scheme only -- a proxy URL usually carries a password."""
    try:
        u = httpx.URL(url)
        return f"{u.scheme}://{u.host}:{u.port or ''}"
    except Exception:  # noqa: BLE001
        return "<proxy>"


def diagnose_connect(host: str, port: int = 443, timeout: float = 6.0) -> str:
    """One line saying what the OS said when we tried to reach host:port.

    Runs synchronously (call it through ``asyncio.to_thread``). Never raises.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        return f"DNS for {host} failed: {e}"
    if not infos:
        return f"DNS for {host} returned no addresses"
    parts = []
    seen = set()
    for family, stype, proto, _, addr in infos:
        ip = addr[0]
        if ip in seen:
            continue
        seen.add(ip)
        s = socket.socket(family, stype, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
            parts.append(f"{ip}: TCP {port} open")
        except socket.timeout:
            parts.append(f"{ip}: no reply in {timeout:.0f}s (SYN dropped -- the portal's "
                         f"firewall is not answering this network)")
        except OSError as e:
            name = _errno.errorcode.get(e.errno, str(e.errno)) if e.errno else type(e).__name__
            parts.append(f"{ip}: {name} ({e.strerror or e})")
        finally:
            s.close()
    return "; ".join(parts)


#: What the run row says when the portal cannot be reached at all. Shown on
#: the GeM Search page, so it has to tell an admin what to do, not what
#: anyio thought.
PROXY_HINT = (
    "GeM did not accept a connection from this server's network. GeM's firewall "
    "drops cloud egress addresses; the same request works from India. Set "
    "GEM_PROXY_URL (or Admin > Platform Settings > gem_proxy_url) to an "
    "HTTP or SOCKS5 proxy with an Indian address and press Search again."
)
