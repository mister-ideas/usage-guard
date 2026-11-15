"""refusal_mode: how a usage refusal is delivered to the agent."""

from unittest.mock import patch

import pytest

from .test_guard import invoke, invoke_moa, make_record


def _text(resp):
    return resp.choices[0].message.content


def test_default_mode_is_the_explanatory_notice(guard):
    out, calls = invoke(guard, make_record(100))
    assert calls == []
    assert out.usage_guard_blocked is True
    assert _text(out) != ""


def test_empty_mode_returns_an_empty_message_without_dispatching(guard):
    out, calls = invoke(guard, make_record(100), refusal_mode="empty")
    assert calls == []
    assert out.usage_guard_blocked is True
    assert _text(out) == ""
    assert not out.choices[0].message.tool_calls
    assert out.usage.total_tokens == 0


def test_empty_mode_still_allows_a_healthy_window(guard):
    out, calls = invoke(guard, make_record(40), refusal_mode="empty")
    assert len(calls) == 1


def test_an_invalid_mode_refuses_with_a_visible_notice(guard):
    out, calls = invoke(guard, make_record(10), refusal_mode="bogus")
    assert calls == []
    assert _text(out) != ""


@pytest.mark.parametrize("mode,expected", [
    ("notice", "notice"), ("empty", "empty"), (" Empty ", "empty"), ("NOTICE", "notice")])
def test_both_modes_are_valid_settings(guard, mode, expected):
    """Case and surrounding spaces are not a misconfiguration."""
    settings, ok = guard._read_settings(lambda k, d: mode if k == "refusal_mode" else d)
    assert ok and settings["refusal_mode"] == expected


@pytest.mark.parametrize("bad", ["", "silent", 1, None])
def test_a_non_mode_value_is_a_config_error(guard, bad):
    _, ok = guard._read_settings(lambda k, d: bad if k == "refusal_mode" else d)
    assert ok is False


def test_a_config_error_keeps_the_notice_even_in_empty_mode(guard):
    """Empty replies must not hide a misconfiguration behind a silent fallback."""
    out, calls = invoke(guard, make_record(10), refusal_mode="empty", threshold_percent="oops")
    assert calls == []
    assert _text(out) == guard._NOTICE_CONFIG
    assert "settings are invalid" in _text(out)


def test_empty_mode_refuses_an_exhausted_moa_slot_with_an_empty_message(guard):
    records = {"openai-codex": make_record(10), "anthropic": make_record(100)}
    out, calls = invoke_moa(guard, records, ["openai-codex", "anthropic"], refusal_mode="empty")
    assert calls == []
    assert out.usage_guard_blocked is True
    assert _text(out) == ""


def test_notice_mode_keeps_the_moa_notice(guard):
    records = {"openai-codex": make_record(10), "anthropic": make_record(100)}
    out, calls = invoke_moa(guard, records, ["openai-codex", "anthropic"])
    assert calls == []
    assert _text(out) != ""


def test_an_unresolvable_moa_preset_keeps_its_notice_in_empty_mode(guard):
    """An unresolvable preset is a configuration problem, so it stays visible."""
    policy = dict(guard.DEFAULTS, refusal_mode="empty")
    with patch.object(guard, "_moa_preset_slots", return_value=None):
        out = guard.gate_llm_execution(
            lambda key, default: policy.get(key, default),
            provider="moa", model="default", api_mode="chat_completions",
            request={}, next_call=lambda req: pytest.fail("dispatched"))
    assert _text(out) == guard._NOTICE_MOA


@pytest.mark.parametrize("api_mode", ["chat_completions", "anthropic_messages", "codex_responses"])
def test_empty_mode_has_no_text_in_any_api_mode(guard, api_mode):
    policy = dict(guard.DEFAULTS, refusal_mode="empty")
    calls = []
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": make_record(100)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        out = guard.gate_llm_execution(
            lambda key, default: policy.get(key, default),
            provider="anthropic", model="offline", api_mode=api_mode,
            request={}, next_call=lambda req: calls.append(req))
    assert calls == []
    assert out.usage_guard_blocked is True
    if api_mode == "chat_completions":
        assert out.choices[0].message.content == ""
    elif api_mode == "anthropic_messages":
        assert not any(getattr(block, "text", "") for block in out.content)
    else:
        assert out.output_text == ""
