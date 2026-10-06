# Contributing

## Ground rules

Do not claim this plugin guarantees subscription safety or prevents extra billing. The Hermes `llm_execution` middleware fails open on exceptions, and its context does not attest the active credential/account. Stale, empty, unavailable and malformed data deny by default (`fail_open: false`); flipping `fail_open` to allow is a documented availability-over-protection decision, never the silent default. Do not import another plugin (the data source is a re-implementation over `agent.account_usage.fetch_account_usage`), scrape auth files, print tokens or log request payloads.

## Workflow

1. Install dev dependencies: `python -m pip install -e '.[dev]'`
2. Run the tests: `python -m pytest tests -v` (host-contract tests skip unless `HERMES_CORE` points at a Hermes checkout).
3. Lint: `ruff check .`
4. Package check: `python scripts/package.py`
5. Manifest checks: `hermes plugins doctor . --ci` and `hermes plugins validate .`

## Tests to add for behavior changes

Add tests for denied unknown, stale and invalid states, config read errors, allowed exact provider behavior, reset-window handling, and each supported API mode. Tests stay offline: mock the fetch/poll boundary, never call the core fetchers live. Host-dependent tests belong in `tests/test_host_contract.py` and must skip cleanly without `HERMES_CORE`.

Explain new privileges and limitations in the README, and add a CHANGELOG.md entry in the same change.

License for contributions: MIT (see LICENSE).
