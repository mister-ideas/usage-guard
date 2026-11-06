"""The status view: /usage-guard and the Desktop chip read what the guard would decide.

The view is cache-only. It must never spend a request against a usage endpoint,
because that budget is shared per account with the guard itself.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

NOW = datetime.now(timezone.utc)
RESET = (NOW + timedelta(hours=2)).isoformat()


def record(*used, fetched_at=None):
    labels = ["Session", "Weekly"]
    return dict(ok=True, fetched_at=fetched_at or NOW.isoformat(), plan=None,
                unavailable_reason=None, codex_rate_blocked=False,
                windows=[dict(label=labels[i], used_percent=u, reset_at=RESET) for i, u in enumerate(used)])


def reader(guard, **settings):
    policy = dict(guard.DEFAULTS)
    policy.update(settings)
    return lambda key, default: policy.get(key, default)


def status(guard, providers, **settings):
    with patch.object(guard, "_read_cache", return_value={"providers": providers}), \
         patch.object(guard, "_spawn_poll_for_source") as poll:
        out = guard.guard_status(reader(guard, **settings))
    assert not poll.called, "the status view must never read a usage endpoint"
    return out


def entry(out, provider):
    return next(p for p in out["providers"] if p["provider"] == provider)


def test_status_reports_each_guarded_provider_with_its_windows(guard):
    out = status(guard, {"anthropic": record(42, 61)})
    anthropic = entry(out, "anthropic")
    assert anthropic["state"] == "ok"
    assert anthropic["peak_percent"] == 61
    assert [(w["label"], w["used_percent"]) for w in anthropic["windows"]] == [("Session", 42), ("Weekly", 61)]
    assert out["threshold_percent"] == guard.DEFAULTS["threshold_percent"]
    assert {p["provider"] for p in out["providers"]} == set(guard.DEFAULTS["provider_keys"])


def test_status_marks_the_near_wall_zone(guard):
    out = status(guard, {"anthropic": record(20, 93)}, near_wall_percent=90)
    assert entry(out, "anthropic")["state"] == "near"


def test_status_says_blocked_with_the_refusal_when_a_window_is_exhausted(guard):
    out = status(guard, {"anthropic": record(100, 40)})
    anthropic = entry(out, "anthropic")
    assert anthropic["state"] == "blocked"
    assert "Session" in anthropic["notice"]
    assert out["state"] == "blocked"


def failed(reason="fetch-error"):
    return dict(ok=False, fetched_at=NOW.isoformat(), windows=[], unavailable_reason=reason)


def test_a_failed_reading_is_blocked_not_ok(guard):
    # Fail closed in the view too: a read that failed means the guard refuses,
    # so the chip must not paint a reassuring state.
    out = status(guard, {"anthropic": failed()})
    assert entry(out, "anthropic")["state"] == "blocked"
    assert entry(out, "anthropic")["windows"] == []
    assert out["state"] == "blocked"


def test_a_failed_reading_under_fail_open_is_unverified(guard):
    out = status(guard, {"anthropic": failed()}, fail_open=True)
    assert entry(out, "anthropic")["state"] == "unverified"


def test_a_provider_never_asked_about_is_idle_and_does_not_colour_the_chip(guard):
    # No record at all means no request has gone to that provider yet (the
    # first one triggers a read before it is sent). A default provider the user
    # never uses must not turn the whole view red.
    out = status(guard, {"anthropic": record(30)})
    assert entry(out, "openai-codex")["state"] == "idle"
    assert out["state"] == "ok"
    assert status(guard, {})["state"] == "idle"


def test_overall_state_is_the_worst_provider(guard):
    out = status(guard, {"anthropic": record(10), "openai-codex": record(95)}, near_wall_percent=90)
    assert out["state"] == "near"


def test_an_old_reading_is_stale_not_blocked(guard):
    # The view never spends a read, but the guard refreshes before the next
    # request. An old reading under the limit must not paint "blocked".
    old = (NOW - timedelta(hours=1)).isoformat()
    out = status(guard, {"anthropic": record(33, 21, fetched_at=old)})
    anthropic = entry(out, "anthropic")
    assert anthropic["state"] == "stale"
    assert anthropic["peak_percent"] == 33 and len(anthropic["windows"]) == 2
    assert out["state"] == "stale"


def test_an_old_reading_over_the_limit_is_still_blocked(guard):
    old = (NOW - timedelta(hours=1)).isoformat()
    assert entry(status(guard, {"anthropic": record(100, fetched_at=old)}), "anthropic")["state"] == "blocked"


def test_slash_command_output_is_plain_text(guard):
    # Desktop shows command output as plain text: no markdown markers.
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": record(30)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        text = guard.format_status(guard.guard_status(reader(guard)))
    assert "`" not in text and "**" not in text


def test_slash_command_prints_a_readable_summary(guard):
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": record(100, 40)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        text = guard.format_status(guard.guard_status(reader(guard)))
    assert "anthropic" in text and "blocked" in text.lower()
    assert "Session" in text and "100%" in text


def test_a_source_without_a_usage_surface_passes_through(guard):
    # The core says the account has no usage surface (an API-key account): the
    # guard lets requests through, and the view says so instead of "blocked".
    out = status(guard, {"anthropic": failed("api-key-account")})
    assert entry(out, "anthropic")["state"] == "passthrough"


def test_slash_command_marks_the_window_over_the_limit_without_repeating_it(guard):
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": record(100, 40)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        text = guard.format_status(guard.guard_status(reader(guard)))
    assert "Session: 100% used" in text and "← at or over the limit" in text
    assert text.count("← at or over the limit") == 1  # the 40% window is not marked
    assert "reason:" not in text  # the mark already says why


def test_slash_command_spells_out_a_failed_reading(guard):
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": failed()}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        text = guard.format_status(guard.guard_status(reader(guard)))
    assert "reason: the usage endpoint did not answer" in text


def test_the_refusal_names_the_threshold_actually_used(guard):
    """A custom limit of 27% must not read "set to refuse at 100%"."""
    st = status(guard, {"anthropic": record(27, 10)}, threshold_percent=27)
    notice = entry(st, "anthropic")["notice"]
    assert "at or over your limit of 27% used" in notice
    assert "set to refuse at" not in notice


def test_refusal_grammar_singular_and_plural(guard):
    one = status(guard, {"anthropic": record(100, 40)})
    both = status(guard, {"anthropic": record(100, 100)})
    one_notice, both_notice = entry(one, "anthropic")["notice"], entry(both, "anthropic")["notice"]
    # Reads as a sentence: "Your Session window is at 100% used (resets …)".
    assert "Your Session window is at 100% used (resets " in one_notice
    assert "Weekly" not in one_notice  # the 40% window is not named
    assert "Your Session and Weekly windows are at 100% and 100% used" in both_notice
    assert "is used up" not in both_notice and "window Session:" not in both_notice


def test_every_setting_the_guard_reads_is_declared_in_the_manifest(guard):
    """A setting missing from config_schema is invisible to settings UIs and validators."""
    import pathlib
    import yaml

    manifest = yaml.safe_load((pathlib.Path(guard.__file__).parent / "plugin.yaml").read_text(encoding="utf-8"))
    assert set(guard.DEFAULTS) <= set(manifest["config_schema"]), set(guard.DEFAULTS) - set(manifest["config_schema"])
