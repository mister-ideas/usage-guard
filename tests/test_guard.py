"""Offline unit tests for the usage-guard middleware (pure plugin behavior).

No network, no Hermes home, no user config: the module is loaded straight
from the repository root and every provider callback is local. Host-dependent
behavior (agent.* imports, auxiliary middleware) lives in test_host_contract.py
and skips when the Hermes tree is not importable.
"""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest

NOW = datetime.now(timezone.utc)
RESET = (NOW + timedelta(hours=2)).isoformat()


def make_record(used=10, *, ok=True, windows=None, fetched_at=None, plan=None,
                unavailable_reason=None, **extra):
    rec = dict(ok=ok, fetched_at=fetched_at or NOW.isoformat(), plan=plan,
               unavailable_reason=unavailable_reason,
               windows=windows if windows is not None else
               [dict(label="Session", used_percent=used, reset_at=RESET)],
               codex_rate_blocked=False)
    rec.update(extra)
    return rec


def snapshot_like(raw=None, used=10, plan=None):
    return NS(fetched_at=NOW, unavailable_reason=None, plan=plan, raw=raw or {},
              windows=[NS(label="Current session", used_percent=used, reset_at=NOW + timedelta(hours=2))])


def invoke(guard, record, provider="anthropic", **settings):
    """Run one middleware gate with a hand-built cache record."""
    policy = dict(guard.DEFAULTS, refresh_every_request=False)
    policy.update(settings)
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {provider: record}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        out = guard.gate_llm_execution(
            lambda key, default: policy.get(key, default),
            provider=provider, model="offline", api_mode="chat_completions",
            request={}, next_call=lambda req: calls.append(req))
    return out, calls


# ── threshold boundary: block exactly at 100% used / 0% left ────────────────

@pytest.mark.parametrize("provider", ["anthropic", "openai-codex"])
@pytest.mark.parametrize("used,allowed", [(0, True), (50, True), (99, True), (99.999, True), (100, False), (101, False)])
def test_default_threshold_blocks_exactly_at_100(guard, provider, used, allowed):
    _, calls = invoke(guard, make_record(used), provider)
    assert (calls == [{}]) is allowed


def test_custom_threshold_is_honored(guard):
    _, calls = invoke(guard, make_record(95), threshold_percent=90)
    assert calls == []
    _, calls = invoke(guard, make_record(89), threshold_percent=90)
    assert calls == [{}]


def test_exhausted_window_among_healthy_ones_denies(guard):
    rec = make_record(10, windows=[dict(label="Session", used_percent=10, reset_at=RESET),
                                   dict(label="Week", used_percent=100, reset_at=RESET)])
    _, calls = invoke(guard, rec)
    assert calls == []


def test_enabled_extra_usage_below_threshold_is_allowed(guard):
    """Account-level extra spending is never gated; only the window threshold."""
    rec = make_record(10, extra_usage_enabled=True)
    _, calls = invoke(guard, rec)
    assert calls == [{}]


# ── malformed / missing evidence denies (fail-closed) ────────────────────────

@pytest.mark.parametrize("rec", [
    dict(ok=False, fetched_at=NOW.isoformat(), windows=[]),
    make_record(10, windows=[]),
    make_record(10, windows=[dict(label="broken", used_percent=float("nan"))]),
    make_record(10, windows=[dict(label="broken", used_percent=-1)]),
    make_record(10, windows=[dict(label="broken", used_percent="10")]),
    make_record(10, windows=["not-a-dict"]),
    make_record(10, fetched_at="not-a-timestamp"),
])
def test_malformed_records_deny(guard, rec):
    out, calls = invoke(guard, rec)
    assert calls == []
    assert out.usage_guard_blocked is True


def test_unavailable_reason_denies_when_fail_closed(guard):
    rec = make_record(10, ok=False, windows=[], unavailable_reason="fetch-error")
    out, calls = invoke(guard, rec)
    assert calls == []
    assert out.usage_guard_blocked is True


def test_fail_open_allows_unavailable(guard):
    rec = make_record(10, ok=False, windows=[], unavailable_reason="fetch-error")
    _, calls = invoke(guard, rec, fail_open=True)
    assert calls == [{}]


# ── stale data and refresh confirmation ─────────────────────────────────────

def test_stale_snapshot_denies_fail_closed(guard):
    old = (NOW - timedelta(seconds=61)).isoformat()
    _, calls = invoke(guard, make_record(10, fetched_at=old), max_cache_age_s=60)
    assert calls == []


def test_stale_snapshot_denies_when_poll_times_out(guard):
    """No refresh lands: the stale path must deny, never dispatch on old data."""
    old = (NOW - timedelta(seconds=61)).isoformat()
    rec = make_record(10, fetched_at=old, polled_at=old)
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": rec}}), \
         patch.object(guard, "_spawn_poll_for_source"):  # returns without writing
        calls = []
        guard.gate_llm_execution(
            lambda key, default: dict(guard.DEFAULTS, refresh_every_request=False).get(key, default),
            provider="anthropic", model="offline", api_mode="chat_completions",
            request={}, next_call=lambda req: calls.append(req))
    assert calls == []


def test_past_reset_denies_until_fresh_confirmation(guard):
    past = (NOW - timedelta(seconds=1)).isoformat()
    rec = make_record(100, windows=[dict(label="Session", used_percent=100, reset_at=past)])
    _, calls = invoke(guard, rec)
    assert calls == []


# ── raw payload ingestion (all published windows reach the gate) ─────────────

@pytest.mark.parametrize("bucket", ["seven_day_oauth_apps", "seven_day_overage_included",
                                    "seven_day_cowork", "seven_day_omelette", "seven_day_future"])
def test_all_raw_weekly_windows_are_ingested(guard, bucket):
    rec = guard._snapshot_record(snapshot_like({bucket: {"utilization": 100, "resets_at": RESET}}))
    assert bucket in [w["label"] for w in rec["windows"]]


def test_limits_array_is_ingested(guard):
    raw = {"limits": [{"kind": "session", "group": "session", "percent": 100, "is_active": True}]}
    rec = guard._snapshot_record(snapshot_like(raw))
    assert any(w["label"].startswith("limit:") for w in rec["windows"])


def test_null_and_inactive_limit_shapes_do_not_crash(guard):
    raw = {"limits": None, "seven_day_opus": None,
           "rate_limit": None, "additional_rate_limits": None}
    rec = guard._snapshot_record(snapshot_like(raw))
    assert rec["ok"] is True


def test_codex_server_rejection_flags_deny(guard):
    for flags in ({"allowed": False, "limit_reached": False}, {"allowed": True, "limit_reached": True}):
        raw = {"rate_limit": dict(flags, primary_window={"used_percent": 10})}
        rec = guard._snapshot_record(snapshot_like(raw))
        assert rec["codex_rate_blocked"] is True
        out, calls = invoke(guard, rec, provider="openai-codex")
        assert calls == []


def test_malformed_raw_values_deny(guard):
    for raw in ({"seven_day_opus": {"utilization": "high"}},
                {"seven_day_opus": {"utilization": -5}},
                {"limits": [{"percent": None}]}):
        rec = guard._snapshot_record(snapshot_like(raw))
        assert rec["ok"] is False


def test_no_usable_windows_deny(guard):
    """Raw payload with only null buckets and no adapter windows denies."""
    snap = NS(fetched_at=NOW, unavailable_reason=None, plan=None,
              raw={"seven_day_opus": None}, windows=[])
    rec = guard._snapshot_record(snap)
    assert rec["ok"] is False
    out, calls = invoke(guard, rec)
    assert calls == []


# ── downstream semantics ─────────────────────────────────────────────────────

def test_downstream_error_propagates_after_dispatch(guard):
    calls = []

    def failing(req):
        calls.append(req)
        raise RuntimeError("already dispatched")

    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": make_record(10)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        with pytest.raises(RuntimeError, match="already dispatched"):
            guard.gate_llm_execution(
                lambda key, default: dict(guard.DEFAULTS, refresh_every_request=False).get(key, default),
                provider="anthropic", model="offline", api_mode="chat_completions",
                request={}, next_call=failing)
    assert len(calls) == 1


def test_non_guarded_provider_passes_through(guard):
    _, calls = invoke(guard, make_record(100), provider="some-other-provider")
    assert calls == [{}]


@pytest.mark.parametrize("api_mode,shape", [
    ("chat_completions", "choices"),
    ("anthropic_messages", "content"),
    ("codex_responses", "output"),
])
def test_blocked_response_shapes(guard, api_mode, shape):
    out = guard._blocked_response(api_mode, "m", "notice")
    assert out.usage_guard_blocked is True
    assert hasattr(out, shape)


def test_unknown_api_mode_still_returns_blocked(guard):
    out = guard._blocked_response("mystery", "m", "notice")
    assert out.usage_guard_blocked is True


def test_malformed_middleware_payload_blocks(guard):
    out = guard.gate_llm_execution(lambda k, d: d, provider="anthropic", model="m",
                                   api_mode="chat_completions", request={}, next_call=None)
    assert out.usage_guard_blocked is True


def test_empty_provider_passes_through(guard):
    calls = []
    guard.gate_llm_execution(lambda k, d: d, provider="", model="m",
                             api_mode="chat_completions", request={}, next_call=calls.append)
    assert len(calls) == 1


# ── config validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,value", [
    ("threshold_percent", 101), ("threshold_percent", -1), ("threshold_percent", "90"),
    ("max_cache_age_s", -5), ("poll_wait_timeout_s", "x"), ("fail_open", "yes"),
    ("refresh_every_request", 1), ("provider_keys", "anthropic"),
    ("provider_sources", {"anthropic": 5}),
])
def test_malformed_settings_deny(guard, key, value):
    settings = dict(guard.DEFAULTS)
    settings["refresh_every_request"] = False
    settings[key] = value
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": make_record(10)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        out = guard.gate_llm_execution(
            lambda k, d: settings.get(k, d), provider="anthropic", model="offline",
            api_mode="chat_completions", request={}, next_call=calls.append)
    assert calls == []
    assert out.usage_guard_blocked is True


def test_stock_defaults_are_setup_agnostic(guard):
    """Shipped defaults must only name stock provider IDs."""
    assert guard.DEFAULTS["provider_keys"] == ["anthropic", "openai-codex"]
    assert guard.DEFAULTS["provider_sources"] == {"anthropic": "anthropic", "openai-codex": "openai-codex"}
    assert guard.DEFAULTS["threshold_percent"] == 100.0
    assert guard.DEFAULTS["fail_open"] is False
    assert guard.DEFAULTS["refresh_every_request"] is True


# M4: lock deadline must be enforceable (LOCK_NB retry), never block forever.

@pytest.mark.skipif(os.name == "nt", reason="posix advisory-lock timeout semantics")
def test_lock_timeout_is_enforced(guard, tmp_path):
    """A lock held by another process must yield TimeoutError within the
    bounded deadline — blocking flock would hang instead of failing."""
    import subprocess
    import sys
    import threading
    import time as _time

    path = str(tmp_path / "usage.json")
    ready = tmp_path / "ready"
    holder_code = (
        "import fcntl, time\n"
        f"f = open({str(path + '.lock')!r}, 'a+')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        f"open({str(ready)!r}, 'w').write('1')\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_code])
    try:
        for _ in range(100):
            if ready.exists():
                break
            _time.sleep(0.05)
        assert ready.exists(), "holder never acquired the lock"
        outcome = {}

        def attempt():
            old = guard._CACHE_LOCK_TIMEOUT_S
            guard._CACHE_LOCK_TIMEOUT_S = 1.0
            try:
                with guard._locked(path):
                    outcome["v"] = "acquired"
            except TimeoutError:
                outcome["v"] = "timeout"
            except Exception as exc:  # noqa: BLE001
                outcome["v"] = f"error:{type(exc).__name__}"
            finally:
                guard._CACHE_LOCK_TIMEOUT_S = old

        t = threading.Thread(target=attempt, daemon=True)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "lock acquisition blocked past its deadline"
        assert outcome.get("v") == "timeout", outcome
    finally:
        holder.terminate()
        holder.wait()


# ── dispatcher interaction: exactly one next_call, never two ─────────────────

def test_config_reader_exception_denies_without_dispatch(guard):
    calls = []

    def bad_config(key, default):
        raise RuntimeError("config exploded")

    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": make_record(10)}}):
        out = guard.gate_llm_execution(bad_config, provider="anthropic", model="m",
                                       api_mode="chat_completions", request={}, next_call=calls.append)
    assert calls == [] and out.usage_guard_blocked is True


def test_denied_path_invokes_next_call_never(guard):
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": make_record(100)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        guard.gate_llm_execution(
            lambda k, d: dict(guard.DEFAULTS, refresh_every_request=False).get(k, d),
            provider="anthropic", model="m", api_mode="chat_completions",
            request={}, next_call=calls.append)
    assert calls == []


def test_allowed_path_invokes_next_call_exactly_once(guard):
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": make_record(10)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        guard.gate_llm_execution(
            lambda k, d: dict(guard.DEFAULTS, refresh_every_request=False).get(k, d),
            provider="anthropic", model="m", api_mode="chat_completions",
            request={}, next_call=calls.append)
    assert len(calls) == 1


# ── conservative scope gating: pinned and documented semantics ───────────────

def test_inactive_limit_is_still_gated(guard):
    """is_active=false limits gate too (conservative; documented in README)."""
    raw = {"limits": [{"kind": "weekly_all", "group": "weekly", "percent": 100,
                       "is_active": False}]}
    rec = guard._snapshot_record(snapshot_like(raw))
    out, calls = invoke(guard, rec)
    assert calls == []


def test_fraction_scale_percent_is_gateable(guard):
    """Live limits[].percent is 0-100 (verified 2026-10-05); 0.5% is legitimate."""
    raw = {"limits": [{"kind": "session", "percent": 0.5, "is_active": True}]}
    rec = guard._snapshot_record(snapshot_like(raw))
    out, calls = invoke(guard, rec, threshold_percent=100)
    assert calls == [{}]  # 0.5% must not trip the 100% threshold


def test_codex_code_review_bucket_gates_inference(guard):
    """Documented conservative behavior: an exhausted code-review window also
    blocks main inference (the guard cannot prove the next model is exempt)."""
    raw = {"code_review_rate_limit": {"primary_window": {"used_percent": 100}},
           "rate_limit": {"allowed": True, "limit_reached": False,
                          "primary_window": {"used_percent": 10}}}
    snap = NS(fetched_at=NOW, unavailable_reason=None, plan=None, raw=raw,
              windows=[NS(label="Weekly", used_percent=10, reset_at=NOW + timedelta(hours=2))])
    out, calls = invoke(guard, guard._snapshot_record(snap), provider="openai-codex")
    assert calls == []


def test_cache_round_trip(guard, tmp_path):
    path = str(tmp_path / "usage.json")
    guard._write_cache({"providers": {"anthropic": make_record(10)}, "schema": 1}, path=path)
    data = guard._read_cache(path=path)
    assert data["providers"]["anthropic"]["windows"][0]["used_percent"] == 10
    assert data["schema"] == 1


def test_cache_read_corrupt_returns_empty(guard, tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("not json{")
    assert guard._read_cache(path=str(path)) == {}


def test_write_failure_is_contained(guard, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    guard._write_cache({"providers": {}}, path=str(blocker / "usage.json"))  # dir is a file: must not raise


def test_manifest_declares_only_middleware():
    import yaml
    manifest = yaml.safe_load(_root().joinpath("plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "usage-guard"
    assert manifest["provides_middleware"] == ["llm_execution"]
    assert manifest["provides_tools"] == [] and manifest["provides_hooks"] == []
    assert manifest.get("requires_env") in (None, [])


def _root():
    from pathlib import Path
    return Path(__file__).resolve().parents[1]


# ── MoA turns (provider="moa"): the running preset's slots are gated ─────────

MOA_RAW = {
    "default_preset": "default",
    "presets": {
        "default": {
            "enabled": True,
            "reference_models": [
                {"provider": "openai-codex", "model": "gpt-6.1-sol", "enabled": True},
                {"provider": "anthropic", "model": "claude-opus-5-5", "enabled": True},
                {"provider": "openrouter", "model": "off", "enabled": False},
            ],
            "aggregator": {"provider": "fireworks", "model": "agg"},
        },
        "other": {
            "enabled": False,
            "reference_models": [{"provider": "anthropic", "model": "claude-opus-5-5"}],
            "aggregator": {"provider": "fireworks", "model": "agg"},
        },
    },
}


def _identity_normalize(raw):
    return raw


def test_moa_slot_providers_lists_enabled_refs_then_aggregator(guard):
    with patch.object(guard, "_normalize_moa_config", side_effect=_identity_normalize):
        assert guard._moa_slot_providers(MOA_RAW, "default") == (
            "default", ["openai-codex", "anthropic", "fireworks"])


def test_moa_slot_providers_skips_disabled_preset_refs(guard):
    with patch.object(guard, "_normalize_moa_config", side_effect=_identity_normalize):
        assert guard._moa_slot_providers(MOA_RAW, "other") == ("other", ["fireworks"])


def test_moa_slot_providers_falls_back_to_default_preset(guard):
    with patch.object(guard, "_normalize_moa_config", side_effect=_identity_normalize):
        assert guard._moa_slot_providers(MOA_RAW, "does-not-exist") == (
            "default", ["openai-codex", "anthropic", "fireworks"])


def test_moa_slot_providers_returns_none_when_unresolvable(guard):
    with patch.object(guard, "_normalize_moa_config", side_effect=RuntimeError("no core")):
        assert guard._moa_slot_providers(MOA_RAW, "default") is None
    with patch.object(guard, "_normalize_moa_config", side_effect=_identity_normalize):
        assert guard._moa_slot_providers({}, "default") is None
        assert guard._moa_slot_providers({"presets": {}}, "default") is None


def invoke_moa(guard, records, providers, model="default", **settings):
    """Run one MoA gate with a hand-built per-provider cache and slot list."""
    policy = dict(guard.DEFAULTS, refresh_every_request=False)
    policy.update(settings)
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": records}), \
         patch.object(guard, "_spawn_poll_for_source"), \
         patch.object(guard, "_moa_preset_slots", return_value=(model, providers)):
        out = guard.gate_llm_execution(
            lambda key, default: policy.get(key, default),
            provider="moa", model=model, api_mode="chat_completions",
            request={}, next_call=lambda req: calls.append(req))
    return out, calls


def test_moa_turn_denied_when_a_guarded_slot_is_exhausted(guard):
    records = {"openai-codex": make_record(10), "anthropic": make_record(100)}
    out, calls = invoke_moa(guard, records, ["openai-codex", "anthropic", "fireworks"])
    assert calls == []
    assert out.usage_guard_blocked is True


def test_moa_turn_allowed_when_guarded_slots_are_healthy(guard):
    records = {"openai-codex": make_record(10), "anthropic": make_record(10)}
    _, calls = invoke_moa(guard, records, ["openai-codex", "anthropic", "fireworks"])
    assert calls == [{}]


def test_moa_turn_ignores_slots_outside_provider_keys(guard):
    _, calls = invoke_moa(guard, {"fireworks": make_record(100)}, ["fireworks"])
    assert calls == [{}]


def test_moa_turn_denied_when_preset_is_unresolvable(guard):
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {}}), \
         patch.object(guard, "_spawn_poll_for_source"), \
         patch.object(guard, "_moa_preset_slots", return_value=None):
        out = guard.gate_llm_execution(
            lambda key, default: dict(guard.DEFAULTS, refresh_every_request=False).get(key, default),
            provider="moa", model="default", api_mode="chat_completions",
            request={}, next_call=lambda req: calls.append(req))
    assert calls == []
    assert out.usage_guard_blocked is True


def test_moa_turn_allows_unresolvable_preset_when_fail_open(guard):
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {}}), \
         patch.object(guard, "_spawn_poll_for_source"), \
         patch.object(guard, "_moa_preset_slots", return_value=None):
        guard.gate_llm_execution(
            lambda key, default: dict(guard.DEFAULTS, refresh_every_request=False,
                                      fail_open=True).get(key, default),
            provider="moa", model="default", api_mode="chat_completions",
            request={}, next_call=lambda req: calls.append(req))
    assert calls == [{}]


# ── Anthropic scale: the host inflates sub-1% utilization ×100 ───────────────
# The OAuth usage payload's ``utilization``/``percent`` values are percentages
# (proved by limits[]: session 4, weekly_all 1). The host fetcher passes
# fraction=True, which multiplies any value <= 1 by 100 — a 1% week arrives as
# 100% and the guard would refuse a healthy account. The plugin reads the
# payload itself and never scales these values.

ANTHROPIC_PAYLOAD = {
    "five_hour": {"utilization": 4.0, "resets_at": "2026-10-16T13:09:59+00:00"},
    "seven_day": {"utilization": 1.0, "resets_at": "2026-10-17T00:59:59+00:00"},
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 4, "severity": "normal",
         "resets_at": "2026-10-16T13:09:59+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 1, "severity": "normal",
         "resets_at": "2026-10-17T00:59:59+00:00", "scope": None, "is_active": False},
    ],
    "extra_usage": {"is_enabled": True, "used_credits": 286.0, "monthly_limit": 50000,
                    "utilization": 0.572, "currency": "EUR"},
}


def anthropic_record(guard, payload=None):
    return guard._snapshot_record(guard._snapshot_like(raw=payload or ANTHROPIC_PAYLOAD))


def test_anthropic_payload_percentages_are_never_scaled(guard):
    """A 1% week stays 1%; nothing becomes 100%."""
    by_label = {w["label"]: w["used_percent"] for w in anthropic_record(guard)["windows"]}
    assert by_label["five_hour"] == 4.0
    assert by_label["seven_day"] == 1.0
    assert 100.0 not in by_label.values()


def test_anthropic_limits_array_is_gated(guard):
    by_label = {w["label"]: w["used_percent"] for w in anthropic_record(guard)["windows"]}
    assert by_label["limit:session"] == 4.0
    assert by_label["limit:weekly_all"] == 1.0


def test_anthropic_healthy_week_passes_the_gate(guard):
    """Regression: a 1% week must not block (the host reports it as 100%)."""
    _, calls = invoke(guard, anthropic_record(guard))
    assert calls == [{}]


def test_anthropic_full_week_still_blocks(guard):
    payload = dict(ANTHROPIC_PAYLOAD,
                   seven_day={"utilization": 100.0, "resets_at": "2026-10-17T00:59:59+00:00"})
    out, calls = invoke(guard, anthropic_record(guard, payload))
    assert calls == []
    assert out.usage_guard_blocked is True


def test_anthropic_snapshot_returns_none_without_the_core(guard):
    """Offline / non-Hermes: no core credential resolver -> no direct read."""
    assert guard._anthropic_snapshot() is None


def _capture_source_fetcher(guard, source_key):
    captured = {}

    def fake_spawn(source_key_, fetcher, sync_timeout_s=0.0):
        captured["source_key"] = source_key_
        captured["fetcher"] = fetcher
        return True

    with patch.object(guard, "_spawn_poll", side_effect=fake_spawn):
        guard._spawn_poll_for_source(source_key)
    return captured


def test_anthropic_source_prefers_the_direct_read(guard):
    captured = _capture_source_fetcher(guard, "anthropic")
    sentinel = NS(raw={"direct": True})
    with patch.object(guard, "_anthropic_snapshot", return_value=sentinel):
        assert captured["fetcher"]() is sentinel


def test_anthropic_source_falls_back_to_the_core_fetcher(guard):
    import sys as _sys
    captured = _capture_source_fetcher(guard, "anthropic")
    core = NS(fetch_account_usage=lambda key: NS(core="anthropic"))
    with patch.object(guard, "_anthropic_snapshot", return_value=None), \
         patch.dict(_sys.modules, {"agent": NS(), "agent.account_usage": core}):
        assert captured["fetcher"]().core == "anthropic"


def test_other_sources_use_the_core_fetcher(guard):
    import sys as _sys
    captured = _capture_source_fetcher(guard, "openai-codex")
    core = NS(fetch_account_usage=lambda key: NS(core=key))
    with patch.object(guard, "_anthropic_snapshot", side_effect=AssertionError("not for codex")), \
         patch.dict(_sys.modules, {"agent": NS(), "agent.account_usage": core}):
        assert captured["fetcher"]().core == "openai-codex"


# ── usage scale: every ingested value is normalized to percent (0-100) ───────
# The host contract is ``AccountUsageWindow.used_percent`` in percent. A source
# that reports fractions must be declared, or a full window (1.0) would read as
# 1% and never block; out-of-range values deny instead of being compared.

def invoke_source(guard, records, provider, **settings):
    """Run one gate with cache records keyed by SOURCE key."""
    policy = dict(guard.DEFAULTS, refresh_every_request=False)
    policy.update(settings)
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": records}), \
         patch.object(guard, "_spawn_poll_for_source"):
        out = guard.gate_llm_execution(
            lambda key, default: policy.get(key, default),
            provider=provider, model="offline", api_mode="chat_completions",
            request={}, next_call=lambda req: calls.append(req))
    return out, calls


def test_fraction_scale_source_blocks_on_a_full_window(guard):
    out, calls = invoke_source(
        guard, {"fractional-src": make_record(1.0)}, provider="alias",
        provider_keys=["alias"], provider_sources={"alias": "fractional-src"},
        source_scales={"fractional-src": "fraction"})
    assert calls == []
    assert out.usage_guard_blocked is True


def test_fraction_scale_source_allows_below_the_threshold(guard):
    _, calls = invoke_source(
        guard, {"fractional-src-2": make_record(0.5)}, provider="alias",
        provider_keys=["alias"], provider_sources={"alias": "fractional-src-2"},
        source_scales={"fractional-src-2": "fraction"})
    assert calls == [{}]


def test_undeclared_source_is_read_as_percent_and_logged(guard, caplog):
    """Percent is the contract default; an all-<=1 undeclared source warns once."""
    import logging
    with caplog.at_level(logging.WARNING, logger="plugins.usage-guard"):
        _, calls = invoke_source(
            guard, {"undeclared-scale-src": make_record(1.0)}, provider="alias",
            provider_keys=["alias"], provider_sources={"alias": "undeclared-scale-src"})
    assert calls == [{}]  # 1.0 read as 1% used, per the contract
    assert any("not declared in source_scales" in r.getMessage() for r in caplog.records)


def test_value_above_one_hundred_denies(guard):
    out, calls = invoke_source(guard, {"anthropic": make_record(101)}, provider="anthropic")
    assert calls == []
    assert out.usage_guard_blocked is True


def test_fraction_scale_value_above_one_denies(guard):
    out, calls = invoke_source(
        guard, {"fractional-src-3": make_record(2.0)}, provider="alias",
        provider_keys=["alias"], provider_sources={"alias": "fractional-src-3"},
        source_scales={"fractional-src-3": "fraction"})
    assert calls == []
    assert out.usage_guard_blocked is True


def test_malformed_source_scales_deny(guard):
    out, calls = invoke_source(guard, {"anthropic": make_record(10)},
                               provider="anthropic", source_scales="percent")
    assert calls == []
    assert out.usage_guard_blocked is True


# ── rate limiting must never refuse a request on its own ────────────────────
# A 429 (or any transport failure) is not evidence about allowance: the last
# good snapshot keeps deciding for up to stale_grace_s, the backoff only
# suppresses new reads, and a failed refresh never erases the evidence.

def _aged(seconds, **kwargs):
    return make_record(fetched_at=(NOW - timedelta(seconds=seconds)).isoformat(), **kwargs)


def test_rate_limited_refresh_with_recent_evidence_allows(guard):
    rec = _aged(120, used=10, unavailable_reason="fetch-error",
                backoff_until=(NOW + timedelta(seconds=300)).isoformat())
    _, calls = invoke(guard, rec)
    assert calls == [{}]


def test_rate_limited_refresh_still_blocks_an_exhausted_window(guard):
    rec = _aged(120, used=100, unavailable_reason="fetch-error",
                backoff_until=(NOW + timedelta(seconds=300)).isoformat())
    out, calls = invoke(guard, rec)
    assert calls == []
    assert out.usage_guard_blocked is True


def test_rate_limited_refresh_without_any_evidence_denies(guard):
    rec = {"ok": False, "windows": [], "fetched_at": None,
           "unavailable_reason": "fetch-error",
           "backoff_until": (NOW + timedelta(seconds=300)).isoformat()}
    out, calls = invoke(guard, rec)
    assert calls == []
    assert out.usage_guard_blocked is True


def test_evidence_older_than_the_grace_window_denies(guard):
    rec = _aged(5000, used=10, unavailable_reason="fetch-error",
                backoff_until=(NOW + timedelta(seconds=300)).isoformat())
    out, calls = invoke(guard, rec, stale_grace_s=900.0)
    assert calls == []
    assert out.usage_guard_blocked is True


def test_grace_window_zero_restores_the_strict_stale_policy(guard):
    rec = _aged(120, used=10, unavailable_reason="fetch-error",
                backoff_until=(NOW + timedelta(seconds=300)).isoformat())
    out, calls = invoke(guard, rec, stale_grace_s=0.0)
    assert calls == []
    assert out.usage_guard_blocked is True


def test_failed_refresh_preserves_the_last_good_windows(guard):
    previous = make_record(used=42)
    exc = NS(response=NS(status_code=429, headers={"Retry-After": "300"}))
    rec = guard._failure_record("anthropic", previous, exc)
    assert rec["windows"] == previous["windows"]
    assert rec["fetched_at"] == previous["fetched_at"]
    assert rec["ok"] is False and rec["unavailable_reason"] == "fetch-error"
    assert guard._parse_ts(rec["backoff_until"]) > NOW


def test_backoff_respects_retry_after_and_grows_on_repeats(guard):
    exc = NS(response=NS(status_code=429, headers={"Retry-After": "300"}))
    first = guard._failure_record("anthropic", make_record(), exc)
    assert (guard._parse_ts(first["backoff_until"]) - NOW).total_seconds() >= 300
    no_header = NS(response=NS(status_code=429, headers={}))
    grown = guard._failure_record(
        "anthropic", {**make_record(), "consecutive_failures": 3}, no_header)
    assert (guard._parse_ts(grown["backoff_until"]) - NOW).total_seconds() >= 120
    assert grown["consecutive_failures"] == 4


def test_min_fetch_interval_skips_a_repeat_read(guard):
    calls = []
    recent = {"last_attempt_at": NOW.isoformat()}
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": recent}}), \
         patch.object(guard, "_spawn_poll", side_effect=lambda *a, **k: calls.append(a)):
        guard._spawn_poll_for_source("anthropic", min_interval_s=30.0)
    assert calls == []
    with patch.object(guard, "_read_cache", return_value={"providers": {}}), \
         patch.object(guard, "_spawn_poll", side_effect=lambda *a, **k: calls.append(a)):
        guard._spawn_poll_for_source("anthropic", min_interval_s=30.0)
    assert len(calls) == 1


def test_no_other_plugin_is_used_as_a_usage_source(guard):
    """Usage comes from Hermes' own providers; no plugin (e.g. quota) is read."""
    from pathlib import Path
    src = Path(guard.__file__).read_text(encoding="utf-8")
    assert "import quota" not in src
    assert "quota_cache" not in src
    assert ".hermes/plugins" not in src and "plugins/quota" not in src
    assert "plugin-data/usage-guard" in src or "_CACHE_DIRNAME" in src
    assert "agent.account_usage" in src
    assert "agent.anthropic_credentials" in src
