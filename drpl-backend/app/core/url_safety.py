"""Outbound fetches reach the public internet only.

Two fetchers take URLs the platform did not choose: the agents' `web_fetch`
tool (the URL is whatever the model writes -- including one a tender PDF told
it to write) and the linked-document downloader (every URL found inside an
uploaded PDF). Both followed redirects to any host, so either could be
pointed at the cloud metadata endpoint, localhost, or the private network the
backend sits on, and hand the response back to the model or into storage.

``public_only_hook`` is an httpx *request* event hook. httpx runs request
hooks for every request it sends, redirects included, so a public URL that
302s to 169.254.169.254 is refused at the hop that goes inward rather than
after the body is read. Resolution happens at request time; a DNS answer that
changes between this check and the connect (rebinding) is outside what a hook
can see and is accepted as residual risk.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class BlockedURLError(Exception):
    """The URL resolves to an address outbound fetches may not reach."""


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_url(url: str) -> None:
    """Raise BlockedURLError unless ``url`` is http(s) to public addresses only."""
    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BlockedURLError(f"Unsupported or malformed URL: {url}")
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        try:
            infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise BlockedURLError(f"Could not resolve {host}: {e}") from e
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
            except ValueError:
                continue
        if not addresses:
            raise BlockedURLError(f"Could not resolve {host}")
    for ip in addresses:
        if not _is_public_ip(ip):
            raise BlockedURLError(
                f"Refusing to fetch {host}: it resolves to a non-public address"
            )


def public_only_hook(request) -> None:
    """httpx request hook: refuse any hop that is not a public address."""
    assert_public_url(str(request.url))
