"""
DRPL Collector - browser profiles.

Crawl4AI's managed-browser mode keeps cookies, localStorage and session state
in a directory you choose. Put that directory on a Railway volume and the IREPS
session survives redeploys -- which is the difference between one OTP a week
and one OTP per deploy.

THE HARD CONSTRAINT (build sheet section 09, and the one people get wrong):
a Chromium ``user_data_dir`` cannot be shared by two processes. ``numReplicas``
must be 1 on the worker that holds this profile. Two replicas means two logins,
two OTP requests racing for one SIM, and both failing. If throughput is ever
the problem, shard by portal into separate single-replica services -- never by
adding replicas to one.

``acquire_profile_lock`` makes that constraint enforce itself rather than
living only in a comment: a second process that tries to use the same profile
directory fails immediately and says why, instead of corrupting the profile and
burning an OTP to find out.
"""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOCK_NAME = ".collector.lock"
_held: set[Path] = set()


class ProfileLocked(Exception):
    """Another process already owns this browser profile."""


def profile_dir(portal: str, base: Optional[str] = None) -> Path:
    """The profile directory for one portal, created if missing."""
    from collector.config import get_settings

    root = Path(base or get_settings().ireps_profile_dir).expanduser()
    path = root if root.name == portal else root / portal
    path.mkdir(parents=True, exist_ok=True)
    return path


def acquire_profile_lock(path: Path) -> None:
    """Claim a profile directory for this process, or raise ProfileLocked.

    Uses O_EXCL file creation, which is atomic on both POSIX and Windows. A
    lock left behind by a killed process is reclaimed when its recorded PID is
    no longer alive -- a stale lock must not need a human to clear it before
    the collector can run again.
    """
    lock = path / _LOCK_NAME
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        owner = _read_pid(lock)
        if owner is not None and _pid_alive(owner) and owner != os.getpid():
            raise ProfileLocked(
                f"Browser profile {path} is held by pid {owner}. A Chromium "
                f"user_data_dir cannot be shared -- run this service with "
                f"numReplicas=1."
            )
        logger.info("profiles: reclaiming stale lock at %s (owner pid %s)", lock, owner)
        try:
            lock.unlink()
        except OSError:
            pass
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)

    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    _held.add(path)


def release_profile_lock(path: Path) -> None:
    lock = path / _LOCK_NAME
    try:
        if lock.exists() and _read_pid(lock) == os.getpid():
            lock.unlink()
    except OSError as e:
        logger.debug("profiles: could not release %s: %s", lock, e)
    _held.discard(path)


def _read_pid(lock: Path) -> Optional[int]:
    try:
        return int(lock.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        # PermissionError means it exists and belongs to somebody else.
        return isinstance(pid, int) and os.name != "nt"
    except Exception:  # noqa: BLE001
        return False


@atexit.register
def _release_all() -> None:
    for path in list(_held):
        release_profile_lock(path)


def browser_config(portal: str, headless: bool = True):
    """A Crawl4AI BrowserConfig bound to this portal's persistent profile.

    Imported lazily so a collector deployed for GeM only never needs Crawl4AI
    or Chromium installed.
    """
    from crawl4ai import BrowserConfig

    from collector.config import get_settings

    s = get_settings()
    path = profile_dir(portal)
    return BrowserConfig(
        headless=headless,
        # use_persistent_context implies use_managed_browser; setting both is
        # what Crawl4AI's own config normalisation expects.
        use_managed_browser=True,
        use_persistent_context=True,
        user_data_dir=str(path),
        browser_type="chromium",
        user_agent=s.user_agent,
        # A tender portal serving PDFs will occasionally try to download one
        # rather than render it; without this the page hangs.
        accept_downloads=False,
        verbose=False,
    )
