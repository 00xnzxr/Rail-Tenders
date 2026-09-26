from app.core.config import get_settings


def test_auto_scoring_defaults():
    s = get_settings()
    assert s.auto_scoring_enabled is True
    assert s.auto_scoring_model == "claude-haiku-4-5"
    assert s.auto_scoring_interval_seconds == 120
    assert s.auto_scoring_batch_size == 25
    assert s.auto_scoring_max_concurrency == 3
    assert s.auto_scoring_max_retries == 3
    assert s.value_threshold_inr == 5_000_000
