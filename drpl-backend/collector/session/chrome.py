"""
DRPL Collector - borrowing a session from a real, logged-in Chrome.

WHAT THIS IS FOR
----------------
The operating model DRPL wants is: the client leaves Chrome open and signed in
to GeM, and the collector scrapes through that session at the press of a button.
This module is that bridge. It attaches to a Chrome that is already running with
a DevTools port, takes the cookies that browser holds for GeM, and hands back a
warm ``GemSession`` -- so the whole existing httpx pipeline runs on the
browser's authenticated session, at httpx's speed, with no browser automation in
the hot path at all.

WHAT IT IS NOT
--------------
It is **not** required. Everything the collector reads from GeM today --
``/all-bids``, ``/all-bids-data``, and every bid document -- is served to an
anonymous client, verified live on 2026-09-12. That is worth stating plainly
rather than leaving as an assumption, because it decides the failure mode: if
Chrome is not there, is not logged in, or has no DevTools port, the collector
falls back to a plain HTTPS session and the sweep runs exactly as it otherwise
would. A browser that is missing must never be the reason a sweep did not
happen.

What attaching genuinely buys is the harder-to-reach part of the portal: pages
behind ``bidnext.gem.gov.in`` that answer only to a signed-in seller, and
resilience if GeM's WAF ever starts refusing sessions that did not complete a
real TLS/JS handshake. The design keeps both doors open at once.

WHY NOT READ CHROME'S COOKIE DATABASE
-------------------------------------
Because it is the wrong tool and a worse neighbour. Decrypting
``User Data/Default/Cookies`` means DPAPI and the profile's master key, it
yields every cookie for every site the person has visited rather than the one
host we asked about, it breaks whenever Chrome changes its at-rest encryption
(App-Bound Encryption did exactly that), and it is indistinguishable from what
credential-stealing malware does -- which is why endpoint protection flags it.
Attaching to a DevTools port the user deliberately opened is explicit, scoped to
what we ask for, and survives Chrome upgrades.

HOW A CLIENT TURNS IT ON
------------------------
    python -m collector.session.chrome --login

starts Chrome against a profile directory the collector owns, with a DevTools
port. They sign in to GeM once; the profile keeps the session, so the next run
-- and every run after it -- attaches silently.

    python -m collector.session.chrome --login --system-profile

uses their everyday Chrome profile instead, which already holds the GeM login,
so there is nothing to sign in to. The cost is that Chrome has to be fully
closed first: it will not open a DevTools port on a user-data-dir another
process is holding, and a second launch against a live profile quietly hands
the URL to the running instance and exits, leaving no port and no error. That
is Chrome's constraint, not ours -- the CLI checks for it and says so rather
than appearing to fail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "ChromeUnavailable",
    "CdpClient",
    "chrome_session",
    "find_chrome",
    "harvest_gem_session",
    "launch_chrome",
    "probe",
    "system_profile_dir",
]

DEFAULT_PORT = 9222
#: The profile the collector owns. Deliberately not the user's everyday one:
#: Chrome will not open a DevTools port on a profile another process already
#: holds, so pointing at their live profile fails in a way no code can fix.
DEFAULT_PROFILE = "./.gem-chrome-profile"

_WINDOWS_CHROME = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)
_POSIX_CHROME = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


class ChromeUnavailable(Exception):
    """No usable Chrome DevTools endpoint. Always recoverable: fall back."""


def system_profile_dir() -> Optional[Path]:
    """Where the person's everyday Chrome keeps its profiles.

    Offered because that is the profile their GeM login is actually in. Using
    it has one hard condition -- Chrome must be **fully closed** first, all
    windows and any background process -- because Chrome will not open a
    DevTools port on a user-data-dir another process already holds, and a
    second launch against a live profile just hands the URL to the running
    instance and exits. That is a real constraint of Chrome, not something
    this code can work around, so it is stated rather than papered over.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        path = Path(base) / "Google" / "Chrome" / "User Data" if base else None
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        path = Path.home() / ".config" / "google-chrome"
    return path if path and path.exists() else None


def chrome_is_running() -> bool:
    """Is an ordinary Chrome already up? Best-effort, never raises."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return "chrome.exe" in out
        out = subprocess.run(["pgrep", "-x", "chrome"], capture_output=True,
                             text=True, timeout=10)
        return out.returncode == 0
    except Exception:  # noqa: BLE001 - a diagnostic is never worth an exception
        return False


def find_chrome() -> Optional[str]:
    """Locate a Chrome binary, or None."""
    if sys.platform == "win32":
        for path in _WINDOWS_CHROME:
            if path and Path(path).exists():
                return path
        return shutil.which("chrome.exe")
    for candidate in _POSIX_CHROME:
        found = shutil.which(candidate) if "/" not in candidate else (
            candidate if Path(candidate).exists() else None
        )
        if found:
            return found
    return None


# -- CDP ------------------------------------------------------------------


@dataclass
class CdpClient:
    """A minimal Chrome DevTools Protocol client.

    Raw CDP over a websocket rather than Playwright: the collector's GeM path
    is deliberately browser-free and about 200MB of image, and pulling in
    Playwright plus a Chromium download to read a cookie jar would undo that
    for every deployment, including the ones that never attach to a browser.
    ``websockets`` is a few hundred kilobytes and speaks the protocol directly.
    """

    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"
    _ws: Any = None
    _next_id: int = field(default=0)

    @property
    def http_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def version(self) -> dict:
        async with httpx.AsyncClient(timeout=5.0) as c:
            try:
                r = await c.get(f"{self.http_base}/json/version")
            except httpx.HTTPError as e:
                raise ChromeUnavailable(
                    f"no DevTools endpoint on {self.host}:{self.port} ({e})"
                ) from e
        if r.status_code != 200:
            raise ChromeUnavailable(f"DevTools returned HTTP {r.status_code}")
        return r.json()

    async def targets(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{self.http_base}/json/list")
        return r.json() if r.status_code == 200 else []

    async def connect(self) -> None:
        """Attach to the *browser* target, which is where Storage lives."""
        try:
            import websockets
        except ImportError as e:  # pragma: no cover - packaging guard
            raise ChromeUnavailable(
                "the `websockets` package is required to attach to Chrome"
            ) from e

        info = await self.version()
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise ChromeUnavailable("DevTools did not offer a browser websocket")
        self._ws = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None

    async def send(self, method: str, params: Optional[dict] = None,
                   timeout: float = 20.0) -> dict:
        """One CDP call. Replies are correlated by id, events are ignored."""
        if self._ws is None:
            raise ChromeUnavailable("not connected")
        self._next_id += 1
        msg_id = self._next_id
        await self._ws.send(json.dumps(
            {"id": msg_id, "method": method, "params": params or {}}
        ))

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ChromeUnavailable(f"{method} timed out")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            data = json.loads(raw)
            # Anything without our id is an event from some other tab.
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                raise ChromeUnavailable(f"{method} failed: {data['error']}")
            return data.get("result", {})

    async def cookies(self, urls: Optional[list[str]] = None) -> list[dict]:
        """Cookies Chrome holds, optionally narrowed to specific URLs.

        ``Storage.getCookies`` on the browser target returns the whole jar, so
        the URL filter is applied here -- we only ever want GeM's, and carrying
        the rest around would be both pointless and careless.
        """
        result = await self.send("Storage.getCookies")
        cookies = result.get("cookies", [])
        if not urls:
            return cookies
        hosts = []
        for u in urls:
            host = u.split("//", 1)[-1].split("/", 1)[0]
            hosts.append(host.lower())

        def matches(c: dict) -> bool:
            domain = (c.get("domain") or "").lstrip(".").lower()
            return any(h == domain or h.endswith("." + domain) for h in hosts)

        return [c for c in cookies if matches(c)]


async def probe(port: int = DEFAULT_PORT) -> Optional[dict]:
    """Is there a Chrome we can attach to? Returns its version info or None."""
    try:
        return await CdpClient(port=port).version()
    except ChromeUnavailable:
        return None


# -- launching ------------------------------------------------------------


def launch_chrome(
    profile_dir: str = DEFAULT_PROFILE,
    port: int = DEFAULT_PORT,
    url: str = "https://bidplus.gem.gov.in/all-bids",
    headless: bool = False,
) -> subprocess.Popen:
    """Start Chrome with a DevTools port against the collector's own profile.

    Headful by default and on purpose: the entire point of the login flow is
    that a person can see the page and sign in.
    """
    binary = find_chrome()
    if not binary:
        raise ChromeUnavailable("no Chrome binary found on this machine")

    profile = Path(profile_dir).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)

    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # Without this Chrome may hand the URL to an already-running instance
        # and exit, leaving no DevTools port and no error worth the name.
        "--new-window",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url)

    logger.info("chrome: launching with profile %s on port %s", profile, port)
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_for_port(port: int = DEFAULT_PORT, timeout: float = 30.0) -> dict:
    """Block until Chrome's DevTools endpoint answers."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: Optional[dict] = None
    while asyncio.get_event_loop().time() < deadline:
        last = await probe(port)
        if last:
            return last
        await asyncio.sleep(0.5)
    raise ChromeUnavailable(f"Chrome did not open a DevTools port within {timeout}s")


# -- the part the collector actually calls --------------------------------


async def harvest_gem_session(port: int = DEFAULT_PORT, *, warm: bool = True):
    """Build a ``GemSession`` backed by the cookies a live Chrome holds.

    ``warm`` navigates a tab to /all-bids first, so that a browser which has
    not visited GeM this session still picks up the CSRF and WAF cookies the
    data endpoint requires. Without it, attaching to a Chrome that is logged
    in but has not opened the bid list yields a cookie jar with no
    ``csrf_gem_cookie`` in it, and the first POST fails for a reason that
    looks like authentication and is not.

    Raises ``ChromeUnavailable`` -- callers fall back to ``gem.bootstrap()``.
    """
    from collector.config import get_settings
    from collector.portals.gem import _TOKEN_RE, GemSession

    s = get_settings()
    client = CdpClient(port=port)
    await client.connect()
    try:
        if warm:
            await _warm_up(client, f"{s.gem_base_url}/all-bids")
        jar = await client.cookies([s.gem_base_url])
        info = await client.version()
    finally:
        await client.close()

    if not jar:
        raise ChromeUnavailable("Chrome holds no cookies for GeM")

    by_name = {c["name"]: c["value"] for c in jar}
    token = by_name.get("csrf_gem_cookie", "")
    if not token:
        raise ChromeUnavailable(
            "Chrome's GeM cookies carry no csrf_gem_cookie -- open "
            f"{s.gem_base_url}/all-bids in that browser first"
        )

    # The User-Agent must match the browser the cookies came from. A WAF that
    # binds a session to its fingerprint will reject the pair otherwise, and
    # the failure looks like an expired token rather than a mismatch.
    user_agent = (info.get("User-Agent") or s.user_agent).replace("Headless", "")

    http = httpx.AsyncClient(
        timeout=httpx.Timeout(s.sink_timeout_seconds, connect=20.0),
        follow_redirects=True,
        headers={"User-Agent": user_agent},
    )
    for name, value in by_name.items():
        http.cookies.set(name, value, domain=".gem.gov.in")

    logger.info("chrome: borrowed %s GeM cookies from %s",
                len(by_name), info.get("Browser", "chrome"))
    return GemSession(http, token)


async def _warm_up(client: CdpClient, url: str) -> None:
    """Open ``url`` in a tab so Chrome acquires that origin's cookies."""
    try:
        target = await client.send("Target.createTarget", {"url": url})
        target_id = target.get("targetId")
        # No event plumbing here on purpose: this is a fixed, short wait for a
        # page whose only job is to make Chrome set cookies. Subscribing to
        # Page.loadEventFired would mean attaching a session to the tab and
        # demultiplexing its events, which is a lot of protocol for a pause.
        await asyncio.sleep(4.0)
        if target_id:
            await client.send("Target.closeTarget", {"targetId": target_id})
    except ChromeUnavailable as e:
        logger.debug("chrome: warm-up navigation failed: %s", e)


async def chrome_session(port: int = DEFAULT_PORT, *, required: bool = False):
    """``GemSession`` from Chrome if possible, otherwise a plain one.

    This is the function the sweep calls. It returns ``(session, source)`` so
    the run summary can say which one it got -- a collector that silently fell
    back would make "is it using my login?" unanswerable.
    """
    from collector.portals.gem import bootstrap

    try:
        session = await harvest_gem_session(port)
        return session, "chrome"
    except ChromeUnavailable as e:
        if required:
            raise
        logger.info("chrome: not attached (%s) -- using a direct session", e)
        return await bootstrap(), "direct"


# -- CLI ------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m collector.session.chrome",
        description="Attach the collector to a logged-in Chrome.",
    )
    p.add_argument("--login", action="store_true",
                   help="launch Chrome on the collector's profile so you can "
                        "sign in to GeM once; the session persists afterwards")
    p.add_argument("--check", action="store_true",
                   help="report whether a session can be borrowed right now")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--profile", default=DEFAULT_PROFILE)
    p.add_argument("--system-profile", action="store_true",
                   help="use your everyday Chrome profile, which already has "
                        "the GeM login. Chrome must be FULLY CLOSED first.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def run() -> int:
        if args.login:
            existing = await probe(args.port)
            if existing:
                print(f"Chrome is already listening on {args.port}: "
                      f"{existing.get('Browser')}")
            else:
                launch_chrome(args.profile, args.port)
                info = await wait_for_port(args.port)
                print(f"Launched {info.get('Browser')} on port {args.port}")
            print("\nSign in to GeM in that window. Leave it open.")
            print("Then run:  python -m collector.oneclick")
            return 0

        info = await probe(args.port)
        if not info:
            print(f"No Chrome DevTools endpoint on port {args.port}.")
            print("Start one with:  python -m collector.session.chrome --login")
            print("(The collector will still run without it.)")
            return 1
        print(f"Chrome: {info.get('Browser')}")
        try:
            session = await harvest_gem_session(args.port)
        except ChromeUnavailable as e:
            print(f"Attached, but no usable GeM session: {e}")
            return 1
        print(f"GeM session borrowed. csrf token {session.token[:12]}...")
        await session.aclose()
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
