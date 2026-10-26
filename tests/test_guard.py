"""Offline unit tests for the usage-guard middleware (pure plugin behavior).

No network, no Hermes home, no user config: the module is loaded straight
from the repository root and every provider callback is local. Host-dependent
behavior (agent.* imports, auxiliary middleware) lives in test_host_contract.py
and skips when the Hermes tree is not importable.
"""
import json
import os
import threading
import time
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
    policy = dict(guard.DEFAULTS)
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


# ── a source with no usage surface is not gated ──────────────────────────────

_OAUTH_ONLY_REASON = "Anthropic account limits are only available for OAuth-backed Claude accounts."


def test_a_source_without_a_usage_surface_is_not_gated(guard):
    """An Anthropic API-key account publishes no subscription limits, so there is
    nothing to guard: refusing would block a healthy account."""
    rec = make_record(0, ok=True, windows=[], unavailable_reason=_OAUTH_ONLY_REASON)
    out, calls = invoke(guard, rec)
    assert calls == [{}]
    assert getattr(out, "usage_guard_blocked", None) is not True


def test_a_source_without_a_usage_surface_is_reported_once(guard, caplog):
    rec = make_record(0, ok=True, windows=[], unavailable_reason=_OAUTH_ONLY_REASON)
    guard._no_usage_surface_warned.clear()
    with caplog.at_level("WARNING", logger="plugins.usage-guard"):
        invoke(guard, rec)
        invoke(guard, rec)
    hits = [r.getMessage() for r in caplog.records if "no account usage to guard" in r.getMessage()]
    assert len(hits) == 1


def test_the_gating_configuration_is_logged_once(guard, caplog):
    """An installed-but-unconfigured guard must be visible in the logs: the
    provider list it runs on is otherwise invisible, so a provider missing from
    it looks like a working guard that never fires."""
    guard._gating_logged = False
    with caplog.at_level("INFO", logger="plugins.usage-guard"):
        invoke(guard, make_record(10))
        invoke(guard, make_record(10))
    hits = [r.getMessage() for r in caplog.records if "gating providers" in r.getMessage()]
    assert len(hits) == 1
    assert "anthropic" in hits[0] and "threshold" in hits[0]


def test_an_ungated_provider_is_named_once(guard, caplog):
    """A provider outside provider_keys passes through untouched, and the reason
    is on record."""
    guard._gating_logged = False
    guard._ungated_logged.clear()
    with caplog.at_level("INFO", logger="plugins.usage-guard"):
        invoke(guard, make_record(10), provider="some-other-provider")
        invoke(guard, make_record(10), provider="some-other-provider")
    hits = [r.getMessage() for r in caplog.records if "is not gated" in r.getMessage()]
    assert len(hits) == 1 and "some-other-provider" in hits[0]


def test_every_own_failure_reason_still_denies(guard):
    """Only a reason the core reports passes; this plugin's own reasons stay fail-closed."""
    for reason in sorted(guard._OWN_UNAVAILABLE_REASONS):
        rec = make_record(10, ok=False, windows=[], unavailable_reason=reason)
        out, calls = invoke(guard, rec)
        assert calls == [], reason
        assert out.usage_guard_blocked is True, reason


# ── durable state: the plugin's own data dir, resolved per call ──────────────

def test_cache_path_uses_the_plugin_data_dir(guard):
    """State lives in the documented per-plugin data dir, which follows the profile."""
    import sys
    import types
    from pathlib import Path

    seen: list[str] = []
    fake = types.ModuleType("plugins.plugin_storage")

    def fake_dir(name):
        seen.append(name)
        return Path("/profile-home/plugin-data") / name

    fake.plugin_data_dir = fake_dir
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    with patch.dict(sys.modules, {"plugins": pkg, "plugins.plugin_storage": fake}):
        assert guard._cache_path() == str(
            Path("/profile-home/plugin-data") / "usage-guard" / "usage.json")
    assert seen == ["usage-guard"]


def test_cache_path_falls_back_to_hermes_home(guard, tmp_path, monkeypatch):
    """Without the storage helper the same layout is derived from HERMES_HOME."""
    import sys

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch.dict(sys.modules, {"plugins.plugin_storage": None}):
        assert guard._cache_path() == os.path.join(
            str(tmp_path), "plugin-data", "usage-guard", "usage.json")


# ── stale data and the refresh policy ─────────────────────────────────────

def test_a_snapshot_inside_the_source_floor_is_not_stale(guard):
    """The freshness window is never shorter than the source's own read floor.

    Between max_cache_age_s (60s) and that floor (180s for anthropic and
    openai-codex) the guard may not refresh: calling the snapshot stale there
    refused every dispatch in the gap while a good snapshot sat on disk.
    """
    old = (NOW - timedelta(seconds=61)).isoformat()
    _, calls = invoke(guard, make_record(10, fetched_at=old), max_cache_age_s=60)
    assert calls == [{}]


def test_snapshot_beyond_the_floor_denies_fail_closed(guard):
    """Past the floor nothing may refresh the record and nothing explains why:
    the guard refuses rather than deciding on evidence it cannot vouch for."""
    old = (NOW - timedelta(seconds=241)).isoformat()
    _, calls = invoke(guard, make_record(10, fetched_at=old), max_cache_age_s=60)
    assert calls == []


def test_snapshot_beyond_the_floor_denies_when_poll_times_out(guard):
    """No refresh lands and no read failure is recorded: the stale path denies."""
    old = (NOW - timedelta(seconds=241)).isoformat()
    rec = make_record(10, fetched_at=old, polled_at=old)
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": rec}}), \
         patch.object(guard, "_spawn_poll_for_source"):  # returns without writing
        calls = []
        guard.gate_llm_execution(
            lambda key, default: dict(guard.DEFAULTS).get(key, default),
            provider="anthropic", model="offline", api_mode="chat_completions",
            request={}, next_call=lambda req: calls.append(req))
    assert calls == []


def test_past_reset_denies_until_fresh_confirmation(guard):
    past = (NOW - timedelta(seconds=1)).isoformat()
    rec = make_record(100, windows=[dict(label="Session", used_percent=100, reset_at=past)])
    _, calls = invoke(guard, rec)
    assert calls == []


# ── an empty read keeps the last good snapshot ──────────────────────────────
# The host reports a rate limit and a missing credential the same way: an empty
# read that raised nothing. Erasing the evidence on either made one 429 refuse
# every later dispatch until a read happened to succeed.

def _seed_previous(guard, monkeypatch, tmp_path, record):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    guard._write_cache({"providers": {"anthropic": dict(record, last_attempt_at=record["fetched_at"])}})


def test_an_empty_read_keeps_the_last_good_snapshot(guard, tmp_path, monkeypatch):
    previous = make_record(10)
    _seed_previous(guard, monkeypatch, tmp_path, previous)
    guard._spawn_poll("anthropic", lambda: None, sync_timeout_s=5.0)
    record = guard._read_cache()["providers"]["anthropic"]
    assert record["windows"] == previous["windows"]
    assert record["fetched_at"] == previous["fetched_at"]
    assert record["unavailable_reason"] == "fetch-error"
    assert record["consecutive_failures"] == 1
    assert record["backoff_until"]


def test_an_empty_read_allows_while_the_window_is_not_exhausted(guard, tmp_path, monkeypatch):
    _seed_previous(guard, monkeypatch, tmp_path, make_record(10))
    guard._spawn_poll("anthropic", lambda: None, sync_timeout_s=5.0)
    decision = guard._decide_provider("anthropic", "anthropic", dict(guard.DEFAULTS), True, spawn_poll=False)
    assert decision.allow is True


def test_an_empty_read_still_denies_an_exhausted_window(guard, tmp_path, monkeypatch):
    """The interlock keeps applying to the snapshot that was carried."""
    _seed_previous(guard, monkeypatch, tmp_path, make_record(100))
    guard._spawn_poll("anthropic", lambda: None, sync_timeout_s=5.0)
    decision = guard._decide_provider("anthropic", "anthropic", dict(guard.DEFAULTS), True, spawn_poll=False)
    assert decision.allow is False


def test_an_empty_read_with_nothing_to_carry_stays_fail_closed(guard, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    guard._spawn_poll("anthropic", lambda: None, sync_timeout_s=5.0)
    record = guard._read_cache()["providers"]["anthropic"]
    assert not record.get("windows")
    decision = guard._decide_provider("anthropic", "anthropic", dict(guard.DEFAULTS), True, spawn_poll=False)
    assert decision.allow is False


def test_the_carried_snapshot_stops_applying_after_the_grace_window(guard, tmp_path, monkeypatch):
    """A credential that never comes back must not be trusted for ever."""
    old = (NOW - timedelta(seconds=1000)).isoformat()
    _seed_previous(guard, monkeypatch, tmp_path, make_record(10, fetched_at=old))
    guard._spawn_poll("anthropic", lambda: None, sync_timeout_s=5.0)
    decision = guard._decide_provider("anthropic", "anthropic", dict(guard.DEFAULTS), True, spawn_poll=False)
    assert decision.allow is False


def test_an_empty_read_pauses_refreshes(guard, tmp_path, monkeypatch):
    """The backoff stops the guard hammering an endpoint that is failing."""
    _seed_previous(guard, monkeypatch, tmp_path, make_record(10))
    guard._spawn_poll("anthropic", lambda: None, sync_timeout_s=5.0)
    spawned = []
    with patch.object(guard, "_spawn_poll_for_source", side_effect=lambda *a, **k: spawned.append(a)):
        guard._decide_provider("anthropic", "anthropic", dict(guard.DEFAULTS), True)
    assert spawned == []


def test_a_read_reporting_no_usage_surface_is_not_turned_into_a_failure(guard, tmp_path, monkeypatch):
    """A core-reported reason with no windows is left as read: that provider is
    expected to pass through, and there was nothing to carry anyway."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    snapshot = NS(fetched_at=NOW, unavailable_reason=_OAUTH_ONLY_REASON, plan=None, raw={}, windows=[])
    guard._spawn_poll("anthropic", lambda: snapshot, sync_timeout_s=5.0)
    record = guard._read_cache()["providers"]["anthropic"]
    assert not record.get("windows")
    assert not record.get("backoff_until")
    assert _OAUTH_ONLY_REASON in (record.get("unavailable_reason"), record.get("reason"))


# ── the opt-in reading of a host-inflated 100 ───────────────────────────────
# The host multiplies any utilization <= 1 by 100, so a window whose true value
# is 1% arrives as exactly 100 — the value a genuinely exhausted window reports
# too. Nothing outside the host can tell them apart, so the interpretation is
# opt-in per source, and it touches only that one value.

def test_an_inflated_100_is_read_as_1_for_a_named_source(guard):
    _, calls = invoke(guard, make_record(100), provider="anthropic",
                      assume_inflated_sources=["anthropic"])
    assert calls == [{}], "the ambiguous 100 is read as 1% used, so the request passes"


def test_a_100_is_still_exhaustion_without_the_opt_in(guard):
    _, calls = invoke(guard, make_record(100), provider="anthropic")
    assert calls == []


def test_the_opt_in_only_touches_exactly_100(guard):
    """Every reading below 100 keeps its meaning: an inflated sub-1% value reads
    high, which errs towards caution rather than towards spending."""
    kept = guard._undo_host_inflation(make_record(99.5))["windows"][0]
    assert kept["used_percent"] == 99.5 and "host_inflation" not in kept
    read = guard._undo_host_inflation(make_record(100))["windows"][0]
    assert read["used_percent"] == 1.0 and read["host_inflation"] is True


def test_the_opt_in_is_per_source(guard):
    _, calls = invoke(guard, make_record(100), provider="openai-codex",
                      assume_inflated_sources=["anthropic"])
    assert calls == [], "openai-codex was not named"


def test_the_opt_in_says_what_it_costs(guard, caplog):
    guard._inflation_warned.clear()
    with caplog.at_level("WARNING", logger="plugins.usage-guard"):
        invoke(guard, make_record(10), provider="anthropic", assume_inflated_sources=["anthropic"])
        invoke(guard, make_record(10), provider="anthropic", assume_inflated_sources=["anthropic"])
    hits = [r.getMessage() for r in caplog.records if "as 1%" in r.getMessage()]
    assert len(hits) == 1 and "genuinely exhausted" in hits[0]


def test_a_malformed_inflated_setting_denies(guard):
    settings, ok = guard._read_settings(lambda k, d: {"no": "list"} if k == "assume_inflated_sources" else d)
    assert ok is False


# ── borrowing a snapshot another reader already made ────────────────────────
# The endpoints are budgeted per account, not per plugin, so a machine with two
# readers overruns the budget. A file the user names can be read instead of
# spending a request — off by default, and the interlock still applies.

def _shared_file(tmp_path, *, used=10.0, fetched_at=None, provider="anthropic"):
    path = tmp_path / "shared-usage.json"
    path.write_text(json.dumps({
        "fetched_at": fetched_at or NOW.isoformat(),
        "providers": {provider: {
            "fetched_at": fetched_at or NOW.isoformat(),
            "windows": [{"label": "Current week", "used_percent": used,
                         "reset_at": RESET}]}}}), encoding="utf-8")
    return path


def _decide_with_borrow(guard, path, record=None, **settings):
    spawned = []
    policy = dict(guard.DEFAULTS, hydrate_from_paths=[str(path)] if path else [], **settings)
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": record} if record else {}}), \
         patch.object(guard, "_spawn_poll_for_source", side_effect=lambda *a, **k: spawned.append(a)):
        decision = guard._decide_provider("anthropic", "anthropic", policy, True)
    return decision, spawned


def test_a_borrowed_snapshot_is_used_instead_of_reading_the_endpoint(guard, tmp_path):
    decision, spawned = _decide_with_borrow(guard, _shared_file(tmp_path, used=10.0))
    assert decision.allow is True
    assert spawned == [], "a borrowed fresh snapshot must not cost a request"


def test_a_borrowed_snapshot_still_denies_an_exhausted_window(guard, tmp_path):
    """The interlock applies to whatever record is read, borrowed included."""
    decision, _ = _decide_with_borrow(guard, _shared_file(tmp_path, used=100.0))
    assert decision.allow is False


def test_a_borrowed_snapshot_outside_the_window_is_ignored(guard, tmp_path):
    old = (NOW - timedelta(hours=1)).isoformat()
    decision, spawned = _decide_with_borrow(guard, _shared_file(tmp_path, used=10.0, fetched_at=old))
    assert len(spawned) == 1, "stale foreign evidence is not used: read the endpoint"


def test_a_borrowed_snapshot_out_of_range_is_rejected(guard, tmp_path):
    """Foreign data on another scale must not read as a free window."""
    decision, spawned = _decide_with_borrow(guard, _shared_file(tmp_path, used=1.0))
    assert decision.allow is True  # 1.0 on our scale is 1% used
    decision, spawned = _decide_with_borrow(guard, _shared_file(tmp_path, used=150.0))
    assert len(spawned) == 1, "an out-of-range window is rejected, not compared"


def test_borrowing_is_off_by_default(guard, tmp_path):
    path = _shared_file(tmp_path, used=10.0)
    decision, spawned = _decide_with_borrow(guard, None)   # no paths configured
    assert len(spawned) == 1, "without hydrate_from_paths the guard reads the endpoint"
    assert path.exists()


def test_an_unreadable_borrow_path_is_ignored(guard, tmp_path):
    decision, spawned = _decide_with_borrow(guard, tmp_path / "does-not-exist.json")
    assert len(spawned) == 1


def test_a_single_borrow_path_may_be_given_as_a_string(guard):
    settings, ok = guard._read_settings(lambda k, d: "/tmp/shared-usage.json" if k == "hydrate_from_paths" else d)
    assert ok is True and settings["hydrate_from_paths"] == ["/tmp/shared-usage.json"]


def test_a_malformed_borrow_setting_denies(guard):
    """A broken policy denies by design, like every other malformed setting."""
    settings, ok = guard._read_settings(lambda k, d: {"not": "a list"} if k == "hydrate_from_paths" else d)
    assert ok is False


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
                lambda key, default: dict(guard.DEFAULTS).get(key, default),
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
    ("provider_keys", "anthropic"),
    ("provider_sources", {"anthropic": 5}),
])
def test_malformed_settings_deny(guard, key, value):
    settings = dict(guard.DEFAULTS)
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
    assert guard.DEFAULTS["source_min_intervals"] == {
        "anthropic": 180.0, "openai-codex": 180.0, "openrouter": 30.0}
    assert "refresh_every_request" not in guard.DEFAULTS


# The lock deadline must be enforceable (LOCK_NB retry), never block forever.

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
            lambda k, d: dict(guard.DEFAULTS).get(k, d),
            provider="anthropic", model="m", api_mode="chat_completions",
            request={}, next_call=calls.append)
    assert calls == []


def test_allowed_path_invokes_next_call_exactly_once(guard):
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": make_record(10)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        guard.gate_llm_execution(
            lambda k, d: dict(guard.DEFAULTS).get(k, d),
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
    """The live limits[].percent scale is 0-100; 0.5% is legitimate."""
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


def test_manifest_declares_exactly_what_register_adds(guard):
    """Catalog rule 6: declared capabilities must match what the plugin registers."""
    import yaml
    manifest = yaml.safe_load(_root().joinpath("plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "usage-guard"
    seen = {"mw": [], "hooks": [], "tools": []}

    class Ctx:
        def get_config(self, key, default=None):
            return default

        def register_middleware(self, kind, fn):
            seen["mw"].append(kind)

        def register_hook(self, name, fn):
            seen["hooks"].append(name)

        def register_tool(self, *a, **k):
            seen["tools"].append(a)

    guard.register(Ctx())
    assert sorted(manifest["provides_middleware"]) == sorted(seen["mw"])
    assert sorted(manifest["provides_hooks"]) == sorted(seen["hooks"])
    assert manifest["provides_tools"] == [] and seen["tools"] == []
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
    policy = dict(guard.DEFAULTS)
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
            lambda key, default: dict(guard.DEFAULTS).get(key, default),
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


# ── usage sources: every source goes through Hermes's own usage route ────────
# The plugin never reads a usage endpoint itself: the core fetcher resolves the
# provider's stored sign-in and attaches it, so no credential ever reaches this
# code (catalog rule 11). The raw-ingestion tests above cover the percentage
# semantics for whatever payload a core fetcher provides.

def _capture_source_fetcher(guard, source_key):
    captured = {}

    def fake_spawn(source_key_, fetcher, sync_timeout_s=0.0, min_interval_s=0.0):
        captured["source_key"] = source_key_
        captured["fetcher"] = fetcher
        return True

    with patch.object(guard, "_spawn_poll", side_effect=fake_spawn):
        guard._spawn_poll_for_source(source_key)
    return captured


def test_every_source_uses_the_core_fetcher(guard):
    import sys as _sys
    core = NS(fetch_account_usage=lambda key: NS(core=key))
    for source in ("anthropic", "openai-codex", "openrouter"):
        captured = _capture_source_fetcher(guard, source)
        with patch.dict(_sys.modules, {"agent": NS(), "agent.account_usage": core}):
            assert captured["fetcher"]().core == source


def test_plugin_holds_no_credential_route(guard):
    """Catalog rule 11: no token handling, no vendor-client impersonation."""
    from pathlib import Path
    src = Path(guard.__file__).read_text(encoding="utf-8")
    for forbidden in ("resolve_anthropic_token", "_is_oauth_token", "claude-code",
                      "api.anthropic.com", "anthropic-beta", "Authorization"):
        assert forbidden not in src, forbidden


def test_plugin_imports_only_documented_core_modules(guard):
    """Catalog rule 9: no imports of private core modules."""
    from pathlib import Path
    src = Path(guard.__file__).read_text(encoding="utf-8")
    for private in ("hermes_cli.moa_config", "agent.anthropic_credentials", "agent.auxiliary",
                    "hermes_cli.plugins", "sys.modules["):
        assert private not in src, private


# ── usage scale: every ingested value is normalized to percent (0-100) ───────
# The host contract is ``AccountUsageWindow.used_percent`` in percent. A source
# that reports fractions must be declared, or a full window (1.0) would read as
# 1% and never block; out-of-range values deny instead of being compared.

def invoke_source(guard, records, provider, **settings):
    """Run one gate with cache records keyed by SOURCE key."""
    policy = dict(guard.DEFAULTS)
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
    rec = _aged(300, used=10, unavailable_reason="fetch-error",
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


def _gate_with_spy(guard, record, **settings):
    """Run one gate, recording every refresh attempt instead of polling."""
    policy = dict(guard.DEFAULTS)
    policy.update(settings)
    spawned = []
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": record}}), \
         patch.object(guard, "_spawn_poll_for_source",
                      side_effect=lambda *a, **k: spawned.append((a, k))):
        out = guard.gate_llm_execution(
            lambda key, default: policy.get(key, default),
            provider="anthropic", model="offline", api_mode="chat_completions",
            request={}, next_call=calls.append)
    return out, calls, spawned


def test_fresh_evidence_does_not_read_the_endpoint(guard):
    """The TTL, not the request, decides when the endpoint is read."""
    _, calls, spawned = _gate_with_spy(guard, make_record(10))
    assert calls == [{}] and spawned == []


def test_stale_evidence_triggers_one_read(guard):
    _, _, spawned = _gate_with_spy(guard, _aged(300, used=10))
    assert len(spawned) == 1


def test_unconfirmed_refresh_still_decides_on_the_kept_windows(guard):
    """A read that fails marks the record; its kept windows still decide."""
    stale = _aged(300, used=10)
    failed = {**stale, "ok": False, "unavailable_reason": "fetch-error"}
    policy = dict(guard.DEFAULTS)
    calls = []
    with patch.object(guard, "_read_cache", side_effect=[
            {"providers": {"anthropic": stale}}, {"providers": {"anthropic": failed}}]), \
         patch.object(guard, "_spawn_poll_for_source"):
        guard.gate_llm_execution(
            lambda k, d: policy.get(k, d), provider="anthropic", model="offline",
            api_mode="chat_completions", request={}, next_call=calls.append)
    assert calls == [{}]


def test_fetch_slot_claim_is_atomic_across_processes(guard, tmp_path, monkeypatch):
    """A second caller sharing HERMES_HOME must not start its own read."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert guard._claim_fetch_slot("anthropic", 30.0) is True
    assert guard._claim_fetch_slot("anthropic", 30.0) is False
    assert guard._claim_fetch_slot("anthropic", 0.0) is True   # floor disabled


def test_concurrent_callers_collapse_into_one_read(guard, tmp_path, monkeypatch):
    """X guarded requests at once share the cache and cause one network read."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reads = []
    barrier = threading.Barrier(8)

    def spy(source_key, fetcher, sync_timeout_s=0.0):
        reads.append(source_key)
        time.sleep(0.2)   # hold the read open so the peers reach the claim
        now = guard._now_utc().isoformat()
        # Mirror the poller exactly: it stamps last_attempt_at when a read
        # finishes, and that stamp is what keeps the interval floor in force for
        # every other caller. A spy that replaced the record without it would
        # let a late caller claim a second read.
        guard._merge_into_cache(source_key, {
            "ok": True, "windows": [], "fetched_at": now,
            "polled_at": now, "last_attempt_at": now})

    def worker():
        barrier.wait(timeout=5)
        guard._spawn_poll_for_source("anthropic", sync_timeout_s=1.0, min_interval_s=30.0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    with patch.object(guard, "_spawn_poll", spy):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    assert reads == ["anthropic"]


@pytest.mark.parametrize("source,expected", [
    ("anthropic", 180.0), ("openai-codex", 180.0), ("openrouter", 30.0),
])
def test_gate_uses_the_per_source_floor(guard, source, expected):
    """Each provider is read no faster than its own safe cadence."""
    policy = dict(guard.DEFAULTS, provider_keys=["alias"],
                  provider_sources={"alias": source})
    record = _aged(3600, used=10)
    with patch.object(guard, "_read_cache", return_value={"providers": {source: record}}), \
         patch.object(guard, "_spawn_poll_for_source") as poll:
        guard.gate_llm_execution(
            lambda k, d: policy.get(k, d), provider="alias", model="offline",
            api_mode="chat_completions", request={}, next_call=lambda r: None)
    assert poll.call_args.kwargs["min_interval_s"] == expected


def test_undeclared_source_falls_back_to_the_global_floor(guard):
    settings = dict(guard.DEFAULTS, source_min_intervals={"custom": 42.0})
    assert guard._source_interval("custom", settings) == 42.0
    assert guard._source_interval("unknown-source", settings) == guard.DEFAULTS["min_fetch_interval_s"]


def test_concurrent_provider_ids_share_one_source_read(guard, tmp_path, monkeypatch):
    """Any number of concurrent sessions on one provider collapse into one read."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reads = []
    barrier = threading.Barrier(6)

    def spy(source_key, fetcher, sync_timeout_s=0.0, min_interval_s=0.0):
        reads.append(source_key)
        now = guard._now_utc().isoformat()
        guard._merge_into_cache(source_key, {"ok": True, "windows": [], "fetched_at": now,
                                             "polled_at": now, "last_attempt_at": now})

    def worker():
        barrier.wait(timeout=5)
        guard._spawn_poll_for_source(
            "anthropic", sync_timeout_s=1.0,
            min_interval_s=guard._source_interval("anthropic", guard.DEFAULTS))

    with patch.object(guard, "_spawn_poll", spy):
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    assert reads == ["anthropic"]


def test_malformed_source_min_intervals_deny(guard):
    out, calls = invoke(guard, make_record(10), source_min_intervals={"anthropic": -5})
    assert calls == []
    assert out.usage_guard_blocked is True


def test_the_plugin_depends_only_on_hermes_public_surfaces(guard):
    """Its imports are the standard library plus Hermes's documented modules: no
    third-party package, no other plugin, and no read outside its own data dir."""
    import ast
    from pathlib import Path

    source = Path(guard.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    hermes = {"agent", "hermes_cli", "plugins"}
    stdlib = {
        "__future__", "contextlib", "datetime", "fcntl", "json", "logging", "math",
        "msvcrt", "os", "threading", "time", "types", "typing", "uuid",
    }
    assert modules <= hermes | stdlib, sorted(modules - (hermes | stdlib))
    assert modules & hermes == hermes  # every Hermes surface it uses is the documented one
    assert "agent.account_usage" in source and "hermes_cli.config" in source
    assert "plugins.plugin_storage" in source
    assert "agent.anthropic_credentials" not in source  # no credential route in the plugin
    assert "plugin-data/usage-guard" in source or "_CACHE_DIRNAME" in source
