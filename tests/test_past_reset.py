"""A window whose reset has passed but whose last reading was under the limit.

The endpoint can stop answering for hours. When it does, a window's ``reset_at``
moves into the past while the snapshot still shows a low percentage — and the
guard refused anyway, so an agent that was nowhere near its limit could not send
a single request until the endpoint answered again. A past reset is not proof
that a low reading cleared: refuse only when the reading was at or over the
limit, the same test ``_exhausted_windows`` already applies.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

NOW = datetime.now(timezone.utc)


def anthropic(session, *, session_reset, session_used=40.0, week_used=27.0):
    return NS(provider="anthropic", source="oauth_usage_api", fetched_at=NOW, unavailable_reason=None,
              plan=None, raw={}, details=(),
              windows=[NS(label="Current session", used_percent=session_used, reset_at=session_reset),
                       NS(label="Current week", used_percent=week_used, reset_at=NOW + timedelta(days=3))])


def read(guard, tmp_path, monkeypatch, snapshot, threshold=100.0):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    guard._spawn_poll("anthropic", lambda: snapshot, sync_timeout_s=5.0)
    settings = dict(guard.DEFAULTS)
    settings["threshold_percent"] = threshold
    return guard._decide_provider("anthropic", "anthropic", settings, True, spawn_poll=False)


def test_a_past_reset_with_room_left_does_not_refuse(guard, tmp_path, monkeypatch):
    decision = read(guard, tmp_path, monkeypatch, anthropic(None, session_reset=NOW - timedelta(hours=2), session_used=30.0))
    assert decision.allow is True, decision.reason


def test_a_past_reset_at_the_limit_still_refuses(guard, tmp_path, monkeypatch):
    decision = read(guard, tmp_path, monkeypatch, anthropic(None, session_reset=NOW - timedelta(hours=2), session_used=100.0))
    assert decision.allow is False
    assert "reset" in decision.reason or "used" in decision.reason


def test_the_threshold_decides_not_the_clock(guard, tmp_path, monkeypatch):
    # Same past reset, same reading: under the limit it passes, at it refuses.
    snapshot = anthropic(None, session_reset=NOW - timedelta(minutes=5), session_used=96.0)
    assert read(guard, tmp_path, monkeypatch, snapshot, threshold=96.5).allow is True
    assert read(guard, tmp_path, monkeypatch, snapshot, threshold=96.0).allow is False
