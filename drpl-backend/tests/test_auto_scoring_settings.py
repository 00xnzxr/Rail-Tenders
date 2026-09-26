from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.platform_setting import PlatformSetting  # noqa: F401 (register table)
from app.services.auto_scoring_settings import get_scoring_settings, set_scoring_setting


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_defaults_from_config_when_unset():
    db = _session()
    s = get_scoring_settings(db)
    assert s["auto_scoring_enabled"] is True
    assert s["segment_discard_below"] == 0.40
    assert s["segment_bidable_at"] == 0.60
    assert s["value_threshold_inr"] == 5_000_000
    assert s["auto_discard_days"] == 7


def test_set_then_get_roundtrip_upserts():
    db = _session()
    set_scoring_setting(db, "segment_bidable_at", 0.70)   # key does not pre-exist -> insert
    set_scoring_setting(db, "auto_scoring_enabled", False)
    s = get_scoring_settings(db)
    assert s["segment_bidable_at"] == 0.70
    assert s["auto_scoring_enabled"] is False
