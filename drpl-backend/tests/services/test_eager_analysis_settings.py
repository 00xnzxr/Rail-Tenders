from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.platform_setting import PlatformSetting
from app.services.eager_analysis_settings import get_eager_analysis_settings

def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e)
    return sessionmaker(bind=e)()

def test_defaults_when_no_overrides():
    s = get_eager_analysis_settings(_db())
    assert s["eager_analysis_enabled"] is False
    assert s["eager_analysis_min_score"] == 0.70
    assert s["eager_analysis_daily_cap"] == 200
    assert s["eager_analysis_queue_ceiling"] == 20

def test_platform_setting_override_and_coercion():
    db = _db()
    db.add(PlatformSetting(key="eager_analysis_enabled", value="true", value_type="string", category="eager_analysis"))
    db.add(PlatformSetting(key="eager_analysis_min_score", value="0.9", value_type="string", category="eager_analysis"))
    db.add(PlatformSetting(key="eager_analysis_daily_cap", value="50", value_type="string", category="eager_analysis"))
    db.commit()
    s = get_eager_analysis_settings(db)
    assert s["eager_analysis_enabled"] is True
    assert s["eager_analysis_min_score"] == 0.9
    assert s["eager_analysis_daily_cap"] == 50
