"""Make a fresh deployment usable without anyone opening a database client.

Run once after the container comes up (or on every boot -- it is idempotent):

    python scripts/seed_demo.py

Three jobs:

1. A master_admin to log in as. Credentials come from ADMIN_EMAIL /
   ADMIN_PASSWORD so nothing is hard-coded here; with no ADMIN_PASSWORD set it
   refuses rather than inventing one, because a generated password printed into
   a container log nobody reads is the same as no account at all.
2. The AI provider settings this deployment actually runs on. `app.seed` writes
   `DEFAULT_SETTINGS`, which name Anthropic; a deployment holding only an
   OpenAI and a Google key needs `ai_provider`, `ai_model` and
   `force_provider_override` pointed somewhere it can reach, or every agent
   resolves to a provider with no credential.
3. The GeM scope profile the collector filters on, when none exists -- without
   it a sweep runs and keeps nothing.

Safe to re-run: every write is compared first, and an existing admin's password
is never reset.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from passlib.context import CryptContext  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models.platform_setting import PlatformSetting  # noqa: E402
from app.models.user import User  # noqa: E402

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _ensure_admin(db) -> str:
    email = (os.environ.get("ADMIN_EMAIL") or "admin@drpl.com").strip()
    password = os.environ.get("ADMIN_PASSWORD") or ""

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        changed = []
        if existing.role != "master_admin":
            existing.role = "master_admin"
            changed.append("role")
        if not existing.is_active:
            existing.is_active = True
            changed.append("is_active")
        if changed:
            db.commit()
            return f"admin {email}: updated {', '.join(changed)}"
        return f"admin {email}: already correct"

    if not password:
        return (
            f"admin {email}: NOT created -- set ADMIN_PASSWORD and re-run. "
            "Refusing to generate one, because a password printed into a "
            "container log is not a password anyone has."
        )

    db.add(User(
        email=email,
        name="DRPL Demo Admin",
        hashed_password=pwd_context.hash(password),
        role="master_admin",
        is_active=True,
    ))
    db.commit()
    return f"admin {email}: created (master_admin)"


#: Settings this deployment needs to differ from DEFAULT_SETTINGS, and why.
#: Values come from the environment so the same script serves a deployment that
#: later gets its Anthropic credit back -- unset means "leave whatever is there".
def _provider_settings() -> dict[str, str]:
    wanted: dict[str, str] = {}
    provider = (os.environ.get("AI_PROVIDER") or "").strip()
    if provider:
        wanted["ai_provider"] = provider
        # The kill-switch documented in settings_service: it overrides every
        # per-agent provider pin at once, which is the point when one provider
        # is unreachable.
        wanted["force_provider_override"] = provider
    model = (os.environ.get("AI_MODEL") or "").strip()
    if model:
        wanted["ai_model"] = model
    for env_key, setting_key in (
        ("OPENAI_API_KEY", "openai_api_key"),
        ("GOOGLE_API_KEY", "google_api_key"),
        ("ANTHROPIC_API_KEY", "anthropic_api_key"),
        ("GEMINI_SEARCH_MODEL", "gemini_search_model"),
    ):
        value = (os.environ.get(env_key) or "").strip()
        if value:
            wanted[setting_key] = value
    return wanted


def _ensure_settings(db) -> list[str]:
    notes: list[str] = []
    for key, value in _provider_settings().items():
        row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
        if row is None:
            notes.append(f"setting {key}: absent, left to seed_defaults")
            continue
        if (row.value or "") == value:
            continue
        row.value = value
        db.add(row)
        notes.append(f"setting {key}: updated"
                     + ("" if row.is_secret else f" -> {value}"))
    if notes:
        db.commit()
    return notes


def _ensure_scope_profile(db) -> str:
    """A collector sweep with no scope profile keeps nothing."""
    try:
        from app.models.tender_scope_profile import TenderScopeProfile
    except Exception as exc:  # noqa: BLE001
        return f"scope profile: skipped ({type(exc).__name__}: {exc})"

    existing = db.query(TenderScopeProfile).first()
    if existing:
        return "scope profile: already present"
    try:
        db.add(TenderScopeProfile(
            name="default",
            # keyword_groups is a list of {label, keywords} -- each keyword is
            # what the GeM driver types into the search box.
            keyword_groups=[
                {"label": "Railways", "keywords": [
                    "railway", "indian railways", "coach", "wagon"]},
                {"label": "Maintenance", "keywords": [
                    "AMC", "annual maintenance"]},
            ],
            exclusion_terms=[],
            target_ministries=["Ministry of Railways"],
            relevance_threshold=0.6,
            is_active=True,
        ))
        db.commit()
        return "scope profile: created 'default' (Ministry of Railways)"
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return f"scope profile: could not create ({type(exc).__name__}: {exc})"


def main() -> int:
    db = SessionLocal()
    try:
        lines = [_ensure_admin(db)]
        lines += _ensure_settings(db)
        lines.append(_ensure_scope_profile(db))
    finally:
        db.close()
    for line in lines:
        print(f"[seed_demo] {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
