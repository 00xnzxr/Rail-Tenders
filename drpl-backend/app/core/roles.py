"""The three roles, and the surfaces each one may reach.

- ``master_admin`` — everything, including all admin settings and every user's
  work. Unchanged.
- ``tender_search`` — the whole platform except ``/admin/*``.
- ``costing_research`` — the costing surface only: Command Center, tenders, the
  dashboard, the per-tender workspace (checklist + annexures), cost breakdowns
  and their XLSX export, the archive, and notifications.

Access is expressed as *surfaces* rather than route paths so the frontend nav
and the backend dependency answer the same question from the same table. A
route path appears once, in the router; the rule appears once, here.

Two things this module is careful about:

**Legacy values keep working.** The platform is live and its users hold ``admin``
and ``operator`` right now. `normalize` maps them forward rather than locking
anyone out on deploy.

**An unrecognised role gets the narrowest surface, never the widest.** A typo in
a role column should cost someone access to a page, not hand them the platform.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, HTTPException

from app.core.auth import get_current_user
from app.models.user import User

MASTER_ADMIN = "master_admin"
TENDER_SEARCH = "tender_search"
COSTING_RESEARCH = "costing_research"

ROLES = (MASTER_ADMIN, TENDER_SEARCH, COSTING_RESEARCH)

#: Old value → new value. `admin` was the whole platform bar admin settings;
#: `operator` was the everyday user.
_LEGACY = {
    "admin": TENDER_SEARCH,
    "operator": COSTING_RESEARCH,
    "user": COSTING_RESEARCH,
}

#: Every gated surface on the platform.
SURFACES = (
    "dashboard",
    "tenders",
    "archive",
    "notifications",
    "command_center",
    "workspace",          # per-tender checklist + annexures
    "cost_breakdown",     # costing + XLSX export
    "documents",          # "My Work": personal uploads and signing
    "signatures",
    "document_generator", # /documents/generate and /documents/library
    "reviews",
    "settings",
    "admin",              # everything under /admin/*
)

#: What each role may reach. master_admin is handled separately — it reaches
#: everything by definition, and enumerating that invites it to drift.
_ALLOWED: dict[str, frozenset[str]] = {
    TENDER_SEARCH: frozenset(s for s in SURFACES if s != "admin"),
    COSTING_RESEARCH: frozenset({
        "dashboard",
        "tenders",
        "archive",
        "notifications",
        "command_center",
        "workspace",
        "cost_breakdown",
        "settings",
    }),
}


def normalize(role: Optional[str]) -> str:
    """Map any stored role value onto one of the three current roles."""
    if role in ROLES:
        return role
    if role in _LEGACY:
        return _LEGACY[role]
    # Unknown or missing: the narrowest surface. Failing wide here would hand a
    # typo'd role column the run of the platform.
    return COSTING_RESEARCH


def can_access(role: Optional[str], surface: str) -> bool:
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r} — add it to SURFACES")
    normalized = normalize(role)
    if normalized == MASTER_ADMIN:
        return True
    return surface in _ALLOWED[normalized]


def require_surface(surface: str) -> Callable[..., User]:
    """FastAPI dependency: 403 unless the caller's role reaches ``surface``.

    The role is read from the user row loaded by `get_current_user`, never from
    anything the client sent. The frontend guard that hides a link is cosmetic —
    `localStorage.drpl_role` is user-editable — so this is the actual control.
    """
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r} — add it to SURFACES")

    def _dependency(current_user: User = Depends(get_current_user)) -> User:
        if not can_access(current_user.role, surface):
            raise HTTPException(
                status_code=403,
                detail=f"Your role does not have access to this area.",
            )
        return current_user

    return _dependency
