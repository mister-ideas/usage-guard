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


def test_core_usage_fetchers_are_registered():
    """The guard's sources must exist as core fetchers (public API)."""
    au = _core_au()
    fetchers = getattr(au, "_USAGE_FETCHERS", {})
    assert "anthropic" in fetchers and "openai-codex" in fetchers


def test_fetch_account_usage_accepts_the_documented_arguments():
    au = _core_au()
    params = inspect.signature(au.fetch_account_usage).parameters
    assert {"provider", "base_url", "api_key"} <= set(params)
    # ``read_only`` is documented on current releases and absent on older ones;
    # the guard calls the positional form, so either shape works.


def test_anthropic_fetcher_scaling_is_a_documented_upstream_caveat(capfd):
    """Upstream's Anthropic parser passes ``fraction=True``.

    The payload's ``utilization`` is a percentage scale (its own ``limits[]``
    reports the same windows as ``percent``), so a sub-1% window arrives
    inflated. The guard reads whatever the core reports and cannot repair it
    from the public API; this probe records the state so the README caveat can
    be dropped when upstream stops scaling.
    """
    au = _core_au()
    src = inspect.getsource(au._fetch_anthropic_account_usage)
    print(f"upstream anthropic fraction=True present: {'fraction=True' in src}")
    assert isinstance("fraction=True" in src, bool)


def test_moa_defaults_mirror_the_core(guard):
    """The guard's local MoA defaults must match the core's own constants."""
    moa = importlib.import_module("hermes_cli.moa_config")
    assert [dict(s) for s in guard._MOA_DEFAULT_REFERENCE_MODELS] == [
        {k: v for k, v in slot.items()} for slot in moa.DEFAULT_MOA_REFERENCE_MODELS]
    assert dict(guard._MOA_DEFAULT_AGGREGATOR) == dict(moa.DEFAULT_MOA_AGGREGATOR)


def test_moa_normalizer_matches_the_core_on_a_sample(guard):
    """The guard's local MoA normalizer agrees with the core's on a real sample."""
    moa = importlib.import_module("hermes_cli.moa_config")
    sample = {
        "default_preset": "deep",
        "presets": {
            "deep": {
                "reference_models": [
                    {"provider": "anthropic", "model": "claude-opus-5-5"},
                    {"provider": "openai-codex", "model": "gpt-6.1-sol", "enabled": False},
                ],
                "aggregator": {"provider": "fireworks", "model": "agg"},
            },
            "off": {"enabled": False, "reference_models": [], "aggregator": {}},
        },
    }
    mine = guard._normalize_moa_config(sample)
    core = moa.normalize_moa_config(sample)
    assert set(mine["presets"]) == set(core["presets"])
    assert mine["default_preset"] == core["default_preset"]
    for name in core["presets"]:
        assert mine["presets"][name]["enabled"] == core["presets"][name]["enabled"], name
        assert ([s["provider"] for s in mine["presets"][name]["reference_models"]]
                == [s["provider"] for s in core["presets"][name]["reference_models"]]), name
        assert (mine["presets"][name]["aggregator"]["provider"]
                == core["presets"][name]["aggregator"]["provider"]), name


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
