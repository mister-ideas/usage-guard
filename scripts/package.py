"""Build an installable zip containing only runtime files and user docs.

Usage: python scripts/package.py  → dist/usage-guard-<version>.zip
"""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
version = next(
    line.split(":", 1)[1].strip().strip('"')
    for line in (root / "plugin.yaml").read_text().splitlines()
    if line.startswith("version:")
)
out = root / "dist"
out.mkdir(exist_ok=True)
archive = out / f"usage-guard-{version}.zip"

files = [root / name for name in ("plugin.yaml", "__init__.py", "README.md", "LICENSE", "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md")]

with ZipFile(archive, "w", ZIP_DEFLATED) as output:
    for path in sorted(files):
        output.write(path, "usage-guard/" + path.relative_to(root).as_posix())
print(archive)
