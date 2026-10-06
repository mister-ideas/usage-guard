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
    manifest = yaml.safe_load(_root().joinpath("plugin.yaml").read_text())
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
