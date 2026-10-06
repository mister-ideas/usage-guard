## Summary

What does this change do?

## Checks

- [ ] `python -m pytest tests -v` passes (host-contract tests may skip without `HERMES_CORE`)
- [ ] `ruff check .` passes
- [ ] `python scripts/package.py` builds and the zip contains only runtime files
- [ ] `hermes plugins doctor . --ci` and `hermes plugins validate .` pass
- [ ] No personal identifiers or machine-specific paths outside LICENSE/author/maintainer lines
- [ ] Behavior changes are covered by tests and noted in CHANGELOG.md
