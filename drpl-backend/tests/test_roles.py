"""The three roles and what each may reach.

`master_admin` is unchanged. `tender_search` is the whole platform except
/admin/*. `costing_research` is the narrow costing surface: Command Center,
tenders, dashboard, per-tender workspace, cost breakdowns and XLSX, archive and
notifications — and explicitly not My Work, signatures, the document
generator/library, or admin.

The legacy `admin` / `operator` values keep working: the platform is live and
users hold them right now.
"""

import pytest
from fastapi import HTTPException

from app.core import roles
from app.models.user import User


def _u(role):
    return User(id=1, email="a@b.co", name="A", hashed_password="x", role=role)


def test_the_three_roles_are_named():
    assert roles.MASTER_ADMIN == "master_admin"
    assert roles.TENDER_SEARCH == "tender_search"
    assert roles.COSTING_RESEARCH == "costing_research"


@pytest.mark.parametrize("legacy,modern", [
    ("admin", roles.TENDER_SEARCH),
    ("operator", roles.COSTING_RESEARCH),
])
def test_legacy_roles_map_forward(legacy, modern):
    """Users hold these values today. They must not lose access on deploy."""
    assert roles.normalize(legacy) == modern


def test_unknown_role_is_the_narrowest(monkeypatch):
    """An unrecognised value must not accidentally grant the wide surface."""
    assert roles.normalize("whatever") == roles.COSTING_RESEARCH
    assert roles.normalize(None) == roles.COSTING_RESEARCH


def test_master_admin_is_not_narrowed():
    assert roles.normalize("master_admin") == roles.MASTER_ADMIN


# --------------------------------------------------------------- surfaces --


def test_costing_research_cannot_reach_the_excluded_surfaces():
    for surface in ("documents", "signatures", "document_generator", "admin"):
        assert not roles.can_access(roles.COSTING_RESEARCH, surface), surface


def test_costing_research_reaches_its_own_work():
    for surface in ("command_center", "tenders", "dashboard", "workspace",
                    "cost_breakdown", "archive", "notifications"):
        assert roles.can_access(roles.COSTING_RESEARCH, surface), surface


def test_tender_search_reaches_everything_but_admin():
    for surface in roles.SURFACES:
        expected = surface != "admin"
        assert roles.can_access(roles.TENDER_SEARCH, surface) is expected, surface


def test_master_admin_reaches_everything():
    for surface in roles.SURFACES:
        assert roles.can_access(roles.MASTER_ADMIN, surface), surface


# ------------------------------------------------------------- dependency --


def test_require_surface_allows_a_permitted_role():
    dep = roles.require_surface("cost_breakdown")
    assert dep(_u("costing_research")) is not None


def test_require_surface_403s_a_forbidden_role():
    dep = roles.require_surface("signatures")
    with pytest.raises(HTTPException) as e:
        dep(_u("costing_research"))
    assert e.value.status_code == 403


def test_require_surface_reads_the_role_from_the_user_row():
    """Never from the request — a client-supplied role is not authorisation."""
    dep = roles.require_surface("admin")
    with pytest.raises(HTTPException):
        dep(_u("tender_search"))
    assert dep(_u("master_admin")) is not None
