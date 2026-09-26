"""
Test isolation.

pydantic-settings reads a real ``.env`` if one is present, so a developer with
live credentials on disk would otherwise get different results from CI -- and a
test that passes only because a real API key happens to be configured is worse
than no test at all.

Note the subtlety this has to work around: ``Settings.Config.env_file`` is
evaluated when the class is DEFINED, so exporting ``COLLECTOR_ENV_FILE`` from a
test is too late -- the path is already baked in. (drpl-backend's own config has
the same property with ``DRPL_ENV_FILE``.) So the model config is rewritten in
place instead, which is the only thing pydantic-settings actually re-reads.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    import collector.config as cfg
    from collector.enrich import ai as ai_mod

    absent = str(tmp_path / "no-such.env")
    monkeypatch.setenv("COLLECTOR_ENV_FILE", absent)
    # The line that actually takes effect.
    monkeypatch.setitem(cfg.Settings.model_config, "env_file", absent)
    # One portal session unless a test asks for the pool: an extra session
    # is a real bootstrap against the live portal.
    monkeypatch.setenv("GEM_SESSIONS", "1")

    for leaky in (
        "ANTHROPIC_API_KEY", "DATABASE_URL", "REDIS_URL",
        "DRPL_SERVICE_TOKEN", "DRPL_API_URL", "IREPS_MOBILE",
    ):
        monkeypatch.delenv(leaky, raising=False)

    cfg.get_settings.cache_clear()
    ai_mod.reset_for_tests()
    yield
    cfg.get_settings.cache_clear()
    ai_mod.reset_for_tests()
