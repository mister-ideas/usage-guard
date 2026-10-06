"""Host-integration tests: run against a real Hermes tree when HERMES_CORE is set.

CI's host-contract job clones a pinned Hermes revision into /tmp/core and sets
HERMES_CORE. Without that variable these tests skip (the pure offline
guarantees live in test_guard.py). The mandatory assertions below run whenever
the core is provided; the two informational probes document upstream state.
"""
import importlib
import inspect
import os
import sys

import pytest

CORE = os.environ.get("HERMES_CORE", "").strip()
pytestmark = pytest.mark.skipif(not CORE, reason="HERMES_CORE not set (point it at a hermes-agent checkout)")

if CORE and str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def _core_au():
    return importlib.import_module("agent.account_usage")


def test_anthropic_snapshot_shape():
    """The core Anthropic fetcher surface the guard relies on."""
    au = _core_au()
    snap = au._snapshot("anthropic", "oauth_usage_api", [], [])
    assert snap.fetched_at is not None
    assert hasattr(snap, "windows") and hasattr(snap, "raw")


def test_codex_fetcher_passes_raw_upstream():
    au = _core_au()
    assert "raw=payload" in inspect.getsource(au._fetch_codex_account_usage)


def test_anthropic_fetcher_raw_state_is_documented(capfd):
    """Known upstream gap: the Anthropic fetcher does NOT pass raw=payload.

    The guard degrades gracefully (classic windows still gate; a one-time
    warning is logged). When upstream starts passing raw, flip this into a
    hard assertion and drop the README caveat.
    """
    au = _core_au()
    has_raw = "raw=payload" in inspect.getsource(au._fetch_anthropic_account_usage)
    print(f"upstream anthropic raw= present: {has_raw}")
    assert has_raw in (True, False)  # documented either way; see README


def test_plugin_imports_inside_runtime_tree(guard):
    assert callable(guard.register)
    assert hasattr(guard, "gate_llm_execution")


def test_aux_middleware_seam_state():
    """Documents the auxiliary limitation: upstream has no enforcement seam.

    The guard never relies on it (see README "Auxiliary calls on stock
    Hermes"); this probe only records what the pinned core provides.
    """
    from agent import auxiliary_hooks as ah

    has_seam = "run_llm_execution_middleware" in inspect.getsource(ah)
    print(f"aux-middleware seam present: {has_seam} (upstream expectation: False)")


def test_moa_config_surface_available():
    """The resolver the guard's provider="moa" branch depends on (stock surface)."""
    from hermes_cli.moa_config import normalize_moa_config

    cfg = normalize_moa_config({
        "default_preset": "default",
        "presets": {"default": {
            "reference_models": [{"provider": "anthropic", "model": "claude-opus-5-5"}],
            "aggregator": {"provider": "fireworks", "model": "agg"},
        }},
    })
    preset = cfg["presets"]["default"]
    assert preset["reference_models"][0]["provider"] == "anthropic"
    assert preset["aggregator"]["provider"] == "fireworks"
