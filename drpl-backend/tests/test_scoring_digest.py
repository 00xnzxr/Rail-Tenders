"""The scoring digest is stable when inputs are unchanged, and rebuilds on change."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
import app.services.seed_scoring_agent as seed


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_digest_static_header_present(monkeypatch):
    db = _session()
    # Stub the distillation LLM call so the test is offline + deterministic.
    monkeypatch.setattr(seed, "_distill", lambda raw, db=None: "DIGEST: railway electrical AMC focus")
    monkeypatch.setattr(seed, "build_digest_inputs", lambda _db: "scope keywords: railway, AMC")

    prompt1 = seed.get_scoring_system_prompt(db)
    # Contains the fixed instruction/output-contract header AND the distilled body
    assert "MATCH:" in prompt1
    assert "REASON:" in prompt1
    assert "DIGEST: railway electrical AMC focus" in prompt1

    # Same inputs → no rebuild → identical output (and _distill not called again)
    calls = {"n": 0}
    def _count(raw, db=None):
        calls["n"] += 1
        return "DIGEST: should-not-be-called"
    monkeypatch.setattr(seed, "_distill", _count)
    prompt2 = seed.get_scoring_system_prompt(db)
    assert prompt2 == prompt1
    assert calls["n"] == 0
