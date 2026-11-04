"""Load the plugin under test from the repository root (no install needed)."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def guard():
    spec = importlib.util.spec_from_file_location("usage_guard_under_test", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path_factory, monkeypatch):
    """Every test runs against a throwaway Hermes home, so a test that reaches
    the real cache path (a poll writes its result there) can never touch the
    developer's own usage cache. Tests that need a specific home still set
    HERMES_HOME themselves; that later setenv wins."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
