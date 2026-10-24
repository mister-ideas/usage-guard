"""The plugin must look dependency-free to Hermes's installer.

Hermes treats any plugin with a ``pyproject.toml`` as a Python-dependency member, so a normal
install asks a consent question and a headless ``plugins install --enable`` cannot enable it.
usage-guard has no runtime dependencies; dev tooling lives in ruff.toml, pytest.ini and
requirements-dev.txt, which the installer ignores.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_pyproject_or_runtime_requirements_at_the_plugin_root():
    assert not (ROOT / "pyproject.toml").exists()
    assert not (ROOT / "requirements.txt").exists()


def test_manifest_declares_no_python_dependencies():
    import yaml
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert not manifest.get("pip_dependencies") and not manifest.get("python_dependencies")


def test_version_lives_only_in_the_manifest():
    import yaml
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert str(manifest["version"]).count(".") == 2


CORE = os.environ.get("HERMES_CORE", "").strip()


@pytest.mark.skipif(not CORE, reason="HERMES_CORE not set (point it at a hermes-agent checkout)")
def test_hermes_installer_sees_no_python_declaration():
    if CORE not in sys.path:
        sys.path.insert(0, CORE)
    declarations = importlib.import_module("pm.plugin_declarations")
    declaration = declarations.read_python_declaration(ROOT)
    # Current core API: a plugin is a dependency "member" only when it has a pyproject
    # that installs something, or requirements. usage-guard has none of these.
    assert declaration.pyproject is None
    assert declaration.requirements == ()
    assert declaration.pyproject_installs is False
