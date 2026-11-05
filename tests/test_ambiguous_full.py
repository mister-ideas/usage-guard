"""A 100% Anthropic reading that may really be 1%.

Hermes' Anthropic parser (until NousResearch/hermes-agent#139916) multiplies any
``utilization`` <= 1 by 100, so a 1% window arrives as 100%. Anthropic reports whole
percentages, so only exactly 100.0 is ambiguous. Refusing on it locked a profile out:
the refusal kept usage at 1%, so every later read said 100% again until the window
reset. Usage never goes down inside one window, so an earlier reading of the same
window above 1% proves a later 100% is real.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

NOW = datetime.now(timezone.utc)
SESSION_RESET = NOW + timedelta(hours=3)
WEEK_RESET = NOW + timedelta(days=3)


def anthropic(session, *, extra=False, session_reset=SESSION_RESET, source="oauth_usage_api"):
    return NS(provider="anthropic", source=source, fetched_at=NOW, unavailable_reason=None, plan=None,
              raw={}, details=("Extra usage: 1.00 / 50.00 USD",) if extra else (),
              windows=[NS(label="Current session", used_percent=session, reset_at=session_reset),
                       NS(label="Current week", used_percent=27.0, reset_at=WEEK_RESET)])


def read(guard, tmp_path, monkeypatch, *snapshots):
    """Poll each snapshot in turn, then decide and report on the result."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for snapshot in snapshots:
        guard._spawn_poll("anthropic", lambda s=snapshot: s, sync_timeout_s=5.0)
    settings = dict(guard.DEFAULTS)
    decision = guard._decide_provider("anthropic", "anthropic", settings, True, spawn_poll=False)
    status = guard._provider_status("anthropic", settings, True, datetime.now(timezone.utc))
    return decision, status


def test_a_lone_100_percent_reading_does_not_refuse(guard, tmp_path, monkeypatch):
    decision, status = read(guard, tmp_path, monkeypatch, anthropic(100.0))
    assert decision.allow is True
    assert "1%" in decision.reason
    assert status["state"] == "unverified"
    assert status["peak_percent"] == 27.0, "the unconfirmed window does not drive the gauge"
    assert [w.get("ambiguous") for w in status["windows"]] == [True, False]


def test_with_extra_usage_on_a_lone_100_percent_still_refuses(guard, tmp_path, monkeypatch):
    # A real 100% would start spending paid credits, so the guard keeps refusing.
    decision, status = read(guard, tmp_path, monkeypatch, anthropic(100.0, extra=True))
    assert decision.allow is False
    assert status["state"] == "blocked"


def test_100_after_a_higher_reading_in_the_same_window_refuses(guard, tmp_path, monkeypatch):
    decision, _ = read(guard, tmp_path, monkeypatch, anthropic(98.0), anthropic(100.0))
    assert decision.allow is False


def test_a_higher_reading_from_an_earlier_window_proves_nothing(guard, tmp_path, monkeypatch):
    earlier = anthropic(98.0, session_reset=SESSION_RESET - timedelta(hours=5))
    decision, _ = read(guard, tmp_path, monkeypatch, earlier, anthropic(100.0))
    assert decision.allow is True


def test_a_repeated_lone_100_percent_stays_unconfirmed(guard, tmp_path, monkeypatch):
    decision, _ = read(guard, tmp_path, monkeypatch, anthropic(100.0), anthropic(100.0))
    assert decision.allow is True


def test_other_usage_sources_are_taken_at_their_word(guard, tmp_path, monkeypatch):
    decision, _ = read(guard, tmp_path, monkeypatch, anthropic(100.0, source="borrowed_file"))
    assert decision.allow is False


def test_below_the_limit_nothing_changes(guard, tmp_path, monkeypatch):
    decision, status = read(guard, tmp_path, monkeypatch, anthropic(40.0))
    assert decision.allow is True and decision.reason == ""
    assert status["state"] == "ok" and status["peak_percent"] == 40.0
