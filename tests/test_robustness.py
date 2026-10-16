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
    policy = dict(guard.DEFAULTS)
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


def test_backoff_alone_never_denies_with_recent_evidence(guard):
    """A 429 suppresses new reads; it must not refuse a request by itself."""
    future = (NOW + timedelta(seconds=120)).isoformat()
    _, calls = invoke(guard, rec(backoff_until=future))
    assert calls == [{}]


def test_backoff_without_usable_evidence_denies(guard):
    """No evidence and a rejected refresh: fail-closed still refuses."""
    future = (NOW + timedelta(seconds=120)).isoformat()
    out, calls = invoke(guard, rec(windows=[], fetched_at=None, backoff_until=future,
                                   unavailable_reason="fetch-error"))
    assert calls == []
    assert out.usage_guard_blocked is True


def test_expired_backoff_allows(guard):
    past = (NOW - timedelta(seconds=1)).isoformat()
    _, calls = invoke(guard, rec(backoff_until=past))
    assert calls == [{}]


# ── refresh policy: TTL-driven; a failed read never refuses on its own ───────

def test_ttl_controls_refresh_not_the_request(guard):
    """A fresh record is not re-read; a record past the TTL is."""
    policy = dict(guard.DEFAULTS, max_cache_age_s=60)
    for fetched, expected in ((NOW, []), (NOW - timedelta(seconds=300), ["anthropic"])):
        spawned = []
        record = rec(fetched_at=fetched.isoformat(), polled_at=fetched.isoformat())
        with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": record}}), \
             patch.object(guard, "_spawn_poll_for_source",
                          side_effect=lambda *a, _sink=spawned, **k: _sink.append(a)):
            guard.gate_llm_execution(
                lambda k, d: policy.get(k, d), provider="anthropic", model="offline",
                api_mode="chat_completions", request={}, next_call=lambda r: None)
        assert [call[0] for call in spawned] == expected


def test_failed_refresh_keeps_deciding_on_the_last_windows(guard):
    """The read failed: the kept windows decide, so the request is not refused."""
    stale = rec(fetched_at=(NOW - timedelta(seconds=300)).isoformat(),
                polled_at=(NOW - timedelta(seconds=300)).isoformat())
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
