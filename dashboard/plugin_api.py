"""Read-only status route for the Desktop chip: GET /api/plugins/usage-guard/status.

It answers from the guard's cache file only, so polling it never spends a read
of a provider's usage endpoint (that budget is shared with the guard itself).
"""

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter

_name = "_usage_guard_status"
_spec = importlib.util.spec_from_file_location(_name, Path(__file__).resolve().parents[1] / "__init__.py")
guard = importlib.util.module_from_spec(_spec)
sys.modules[_name] = guard
_spec.loader.exec_module(guard)

router = APIRouter()


@router.get("/status")
def status() -> dict:
    return guard.guard_status(guard.hermes_config_reader())
