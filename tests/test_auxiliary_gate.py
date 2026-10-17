"""Auxiliary gating: same decision as the main loop, refusal marked for the host."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

NOW = datetime.now(timezone.utc)


def rec(used):
    return dict(ok=True, fetched_at=NOW.isoformat(), plan=None, unavailable_reason=None,
                codex_rate_blocked=False,
                windows=[dict(label="Session", used_percent=used,
                              reset_at=(NOW + timedelta(hours=1)).isoformat())])


def run(guard, used, provider="anthropic"):
    calls = []
    cfg = dict(guard.DEFAULTS)
    with patch.object(guard, "_read_cache", return_value={"providers": {provider: rec(used)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        out = guard.gate_auxiliary_execution(
            lambda k, d: cfg.get(k, d), provider=provider, model="m", api_mode="chat_completions",
            aux_task="compression", request={}, next_call=lambda r: calls.append(r) or "ANSWER")
    return out, calls


def test_allowed_attempt_reaches_the_provider(guard):
    out, calls = run(guard, 50)
    assert out == "ANSWER" and calls == [{}]


def test_exhausted_window_refuses_and_marks_the_response(guard):
    out, calls = run(guard, 100)
    assert calls == []
    assert out.usage_guard_blocked is True and out.auxiliary_refused is True


def test_unguarded_provider_passes_through(guard):
    out, calls = run(guard, 100, provider="openrouter")
    assert out == "ANSWER" and calls == [{}]


def test_main_loop_refusal_is_not_marked_as_auxiliary(guard):
    cfg = dict(guard.DEFAULTS)
    with patch.object(guard, "_read_cache", return_value={"providers": {"anthropic": rec(100)}}), \
         patch.object(guard, "_spawn_poll_for_source"):
        out = guard.gate_llm_execution(
            lambda k, d: cfg.get(k, d), provider="anthropic", model="m", api_mode="chat_completions",
            request={}, next_call=lambda r: "ANSWER")
    assert out.usage_guard_blocked is True and not hasattr(out, "auxiliary_refused")


def test_registers_auxiliary_only_when_the_host_has_the_kind(guard):
    seen = []

    class Ctx:
        def get_config(self, key, default=None):
            return default

        def register_middleware(self, kind, fn):
            seen.append(kind)

        def register_hook(self, name, fn):
            pass

    with patch.object(guard, "host_supports_auxiliary_execution", return_value=False):
        guard.register(Ctx())
    assert "auxiliary_execution" not in seen
    seen.clear()
    with patch.object(guard, "host_supports_auxiliary_execution", return_value=True):
        guard.register(Ctx())
    assert "auxiliary_execution" in seen
