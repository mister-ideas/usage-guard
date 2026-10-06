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
