"""refusal_mode: how a usage refusal is delivered to the agent."""

from unittest.mock import patch

import pytest

from .test_guard import invoke, make_record


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


@pytest.mark.parametrize("mode", ["notice", "empty"])
def test_both_modes_are_valid_settings(guard, mode):
    settings, ok = guard._read_settings(lambda k, d: mode if k == "refusal_mode" else d)
    assert ok and settings["refusal_mode"] == mode


def test_a_config_error_keeps_the_notice_even_in_empty_mode(guard):
    """Empty replies must not hide a misconfiguration behind a silent fallback."""
    out, calls = invoke(guard, make_record(10), refusal_mode="empty", threshold_percent="oops")
    assert calls == []
    assert _text(out) != ""


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
