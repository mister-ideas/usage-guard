"""TDD batch 2: dead-branch removal (m1), Codex ISO resets (m2), cache locking
(M4), 429 backoff (M1), and polled_at refresh confirmation (m4)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch
import threading


NOW = datetime.now(timezone.utc)


def rec(**extra):
    base = dict(ok=True, fetched_at=NOW.isoformat(), plan=None, unavailable_reason=None,
                windows=[dict(label="Session", used_percent=10,
                              reset_at=(NOW + timedelta(hours=2)).isoformat())],
                codex_rate_blocked=False, polled_at=NOW.isoformat())
    base.update(extra)
    return base


def invoke(guard, record, provider="anthropic", **settings):
    policy = dict(guard.DEFAULTS, refresh_every_request=False)
    policy.update(settings)
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {provider: record}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        out = guard.gate_llm_execution(
            lambda k, d: policy.get(k, d), provider=provider, model="offline",
            api_mode="chat_completions", request={}, next_call=calls.append)
    return out, calls


# ── m1: _exhausted_windows reports past-reset exhausted windows ──────────────

def test_exhausted_windows_includes_past_reset_window(guard):
    past = (NOW - timedelta(seconds=5)).isoformat()
    hits = guard._exhausted_windows(
        rec(windows=[dict(label="Session", used_percent=100, reset_at=past)]), 100, now=NOW)
    assert len(hits) == 1


# ── m2: Codex reset_at accepts ISO strings as well as unix seconds ───────────

def test_codex_iso_reset_at_is_accepted(guard):
    raw = {"rate_limit": {"allowed": True, "limit_reached": False,
                          "primary_window": {"used_percent": 10,
                                             "reset_at": (NOW + timedelta(hours=1)).isoformat()}}}
    snap = NS(fetched_at=NOW, unavailable_reason=None, plan=None, raw=raw,
              windows=[NS(label="Weekly", used_percent=10, reset_at=NOW + timedelta(hours=1))])
    out = guard._snapshot_record(snap)
    assert out["ok"] is True
    assert any(w["label"].startswith("codex:rate_limit") for w in out["windows"])


# ── M4: locked, unique-tmp cache updates must not lose concurrent writers ────

def test_concurrent_cache_updates_preserve_all_providers(guard, tmp_path):
    path = str(tmp_path / "usage.json")
    guard._write_cache({"providers": {}, "schema": 1}, path=path)
    errors = []

    def update(key):
        try:
            for _ in range(20):
                record = (guard._read_cache(path=path).get("providers", {}).get(key)
                          or {"n": 0})
                record["n"] += 1
                guard._merge_into_cache(key, record, path=path)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=update, args=(f"p{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = guard._read_cache(path=path)
    assert sum(v["n"] for v in data["providers"].values()) == 80, data["providers"]


def test_no_fixed_tmp_collision(guard, tmp_path):
    seen = set()
    real_replace = guard.os.replace

    def spy_replace(src, dst):
        seen.add(src)
        return real_replace(src, dst)

    path = str(tmp_path / "usage.json")
    with patch.object(guard.os, "replace", spy_replace):
        guard._write_cache({"providers": {}}, path=path)
        guard._write_cache({"providers": {}}, path=path)
    assert len(seen) == 2


# ── M1: usage-endpoint 429 → bounded backoff that DENIES (fail-closed) ───────

class FakeRateLimit(Exception):
    def __init__(self):
        super().__init__("429")
        self.response = type("R", (), {"status_code": 429, "headers": {"Retry-After": "30"}})()


def test_poll_429_records_backoff(guard):
    merged = {}

    def fake_merge(source_key, record, path=None):
        merged[source_key] = record

    with patch.object(guard, "_merge_into_cache", fake_merge):
        guard._spawn_poll("anthropic", lambda: (_ for _ in ()).throw(FakeRateLimit()),
                          sync_timeout_s=5)
    cached = merged["anthropic"]
    assert cached.get("backoff_until")


def test_backoff_until_denies(guard):
    future = (NOW + timedelta(seconds=120)).isoformat()
    out, calls = invoke(guard, rec(backoff_until=future))
    assert calls == []
    assert out.usage_guard_blocked is True


def test_expired_backoff_allows(guard):
    past = (NOW - timedelta(seconds=1)).isoformat()
    _, calls = invoke(guard, rec(backoff_until=past))
    assert calls == [{}]


# ── m4: refresh confirmation via polled_at, not fetched_at equality ──────────

def test_new_polled_at_confirms_refresh_even_with_same_fetched_at(guard):
    """Poll lands within the same second → same fetched_at, new polled_at:
    the refresh IS confirmed and the request must pass."""
    old = (NOW - timedelta(seconds=30)).isoformat()
    record = rec(fetched_at=old, polled_at=old)
    landed = rec(fetched_at=old, polled_at=(NOW + timedelta(milliseconds=5)).isoformat())
    policy = dict(guard.DEFAULTS, refresh_every_request=True)
    calls = []
    with patch.object(guard, "_read_cache", side_effect=[
            {"providers": {"anthropic": record}}, {"providers": {"anthropic": landed}}]), \
         patch.object(guard, "_spawn_poll_for_source"):
        guard.gate_llm_execution(
            lambda k, d: policy.get(k, d), provider="anthropic", model="offline",
            api_mode="chat_completions", request={}, next_call=calls.append)
    assert calls == [{}]


def test_unchanged_polled_at_denies(guard):
    old = (NOW - timedelta(seconds=30)).isoformat()
    record = rec(fetched_at=old, polled_at=old)
    policy = dict(guard.DEFAULTS, refresh_every_request=True)
    calls = []
    with patch.object(guard, "_read_cache", side_effect=[
            {"providers": {"anthropic": record}}, {"providers": {"anthropic": record}}]), \
         patch.object(guard, "_spawn_poll_for_source"):
        guard.gate_llm_execution(
            lambda k, d: policy.get(k, d), provider="anthropic", model="offline",
            api_mode="chat_completions", request={}, next_call=calls.append)
    assert calls == []
