from app.core.config import get_settings


def test_segment_config_defaults():
    s = get_settings()
    assert s.segment_discard_below == 0.40
    assert s.segment_bidable_at == 0.60
    assert s.auto_discard_enabled is True
    assert s.auto_discard_days == 7
    assert s.auto_discard_interval_hours == 24
