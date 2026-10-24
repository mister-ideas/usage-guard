"""Near-the-wall behaviour: request cap, early notice, refusal wording."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

NOW = datetime.now(timezone.utc)
RESET = (NOW + timedelta(hours=2)).isoformat()


def record(used, fetched_at=None):
    return dict(ok=True, fetched_at=fetched_at or NOW.isoformat(), plan=None,
                unavailable_reason=None, codex_rate_blocked=False,
                windows=[dict(label="Session", used_percent=used, reset_at=RESET)])


def reader(guard, **settings):
    policy = dict(guard.DEFAULTS)
    policy.update(settings)
    return lambda key, default: policy.get(key, default)


def shape(guard, used, request, provider="anthropic", **settings):
    with patch.object(guard, "_read_cache", return_value={"providers": {provider: record(used)}}):
        return guard.shape_llm_request(
            reader(guard, **settings), provider=provider, session_id="s1", request=request)


def notice(guard, used, session="n1", provider="anthropic", **settings):
    with patch.object(guard, "_read_cache", return_value={"providers": {provider: record(used)}}):
        guard._remember_session_provider(session, provider)
        return guard.near_wall_notice(reader(guard, **settings), session_id=session)


def test_cap_is_off_by_default(guard):
    assert shape(guard, 95, {"max_tokens": 8000}) is None


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
def test_cap_lowers_an_existing_budget_near_the_wall(guard, key):
    out = shape(guard, 95, {key: 8000, "model": "m"}, max_tokens_near_wall=1000)
    assert out["request"][key] == 1000 and out["request"]["model"] == "m"
    assert out["source"] == "usage-guard"


def test_cap_never_adds_a_missing_key_or_raises_a_budget(guard):
    assert shape(guard, 95, {"model": "m"}, max_tokens_near_wall=1000) is None
    assert shape(guard, 95, {"max_tokens": 500}, max_tokens_near_wall=1000) is None


def test_cap_is_inactive_below_the_zone_and_at_the_wall(guard):
    assert shape(guard, 80, {"max_tokens": 8000}, max_tokens_near_wall=1000) is None
    assert shape(guard, 100, {"max_tokens": 8000}, max_tokens_near_wall=1000) is None


def test_cap_ignores_unguarded_providers_and_stale_evidence(guard):
    assert shape(guard, 95, {"max_tokens": 8000}, provider="openrouter", max_tokens_near_wall=1000) is None
    old = (NOW - timedelta(hours=3)).isoformat()
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": record(95, old)}}):
        assert guard.shape_llm_request(
            reader(guard, max_tokens_near_wall=1000), provider="anthropic", session_id="s2",
            request={"max_tokens": 8000}) is None


def test_cap_never_raises_on_a_hostile_payload(guard):
    assert guard.shape_llm_request(reader(guard), provider=None, request="x") is None
    assert guard.shape_llm_request(lambda k, d: 1 / 0, provider="anthropic", request={}) is None


def test_malformed_new_settings_deny_like_the_others(guard):
    for key, bad in (("warn_percents", "80"), ("warn_percents", [0]), ("near_wall_percent", 150),
                     ("max_tokens_near_wall", -1), ("max_tokens_near_wall", True)):
        _, ok = guard._read_settings(reader(guard, **{key: bad}))
        assert ok is False, key


def test_notice_fires_once_per_level_and_again_on_a_higher_one(guard):
    assert notice(guard, 50) is None
    first = notice(guard, 85)
    assert first and "85% used" in first["context"]
    assert notice(guard, 86) is None
    second = notice(guard, 92)
    assert second and "92% used" in second["context"]
    assert notice(guard, 95) is None


def test_notice_rearms_after_a_reset(guard):
    assert notice(guard, 85, session="n2")
    assert notice(guard, 10, session="n2") is None
    assert notice(guard, 85, session="n2")


def test_notice_off_when_levels_empty_and_without_provider(guard):
    assert notice(guard, 95, session="n3", warn_percents=[]) is None
    assert guard.near_wall_notice(reader(guard), session_id="never-seen") is None
    assert guard.near_wall_notice(reader(guard), session_id=None) is None


def test_refusal_tells_autonomous_loops_to_pause(guard):
    assert "Autonomous runs" in guard._NOTICE_REFUSED
    assert "pause until the reset" in guard._NOTICE_REFUSED
    assert "usage_guard_blocked" not in guard._NOTICE_REFUSED


def test_register_adds_request_shaper_and_notice_hook(guard):
    seen = {"mw": [], "hooks": []}

    class Ctx:
        def get_config(self, key, default=None):
            return default

        def register_middleware(self, kind, fn):
            seen["mw"].append(kind)

        def register_hook(self, name, fn):
            seen["hooks"].append(name)

    guard.register(Ctx())
    assert seen == {"mw": ["llm_execution", "llm_request"], "hooks": ["pre_llm_call"]}
