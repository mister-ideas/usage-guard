# Contributing

Thanks for looking. This is a single-maintainer project, so a little structure goes a long way — this file is the single source of truth for how contributions work.

## Before you open anything

| You want to | Go to |
|---|---|
| Report a bug | The [bug report form](https://github.com/mister-ideas/usage-guard/issues/new/choose) |
| Ask how something works, or float an idea | [Discussions](https://github.com/mister-ideas/usage-guard/discussions) |
| Report a vulnerability | [SECURITY.md](SECURITY.md) — privately, never a public issue |
| Report unacceptable behaviour | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |

A bug report needs the refusal notice the guard printed, your Hermes and plugin versions, and the relevant `plugins.usage-guard` log lines. Without that evidence a report usually cannot be reproduced, and it will be closed with a request for it rather than left open.

## Scope: what this plugin will and will not do

**In scope:** gating Hermes's own provider dispatches (`llm_execution`) on the subscription usage Hermes already reads; the refresh and rate-limit policy; the cache; the refusal wording; documentation.

**Out of scope.** Requests for these are closed with a pointer rather than a debate:

- governing anything outside Hermes — other applications, direct CLI runs, claude.ai, editor extensions;
- reading credentials or calling a provider's usage endpoint from the plugin. Hermes resolves sign-ins and attaches them; a plugin that holds a token cannot be listed in the catalog;
- enforcing or changing account and billing settings;
- guarantees about money. The middleware fails open on exceptions, its context does not attest which credential a request will use, and no percentage can predict the next request's cost.

## Ground rules

Do not claim this plugin guarantees subscription safety or prevents extra billing. Stale, empty, unavailable and malformed data deny by default (`fail_open: false`); flipping `fail_open` to allow is a documented availability-over-protection decision, never the silent default. Do not import another plugin (the data source is the host's own `agent.account_usage.fetch_account_usage`), scrape auth files, print tokens or log request payloads. Tests stay offline: mock the fetch/poll boundary, never call the core fetchers live.

## Development workflow

1. Install dev dependencies: `python -m pip install -e '.[dev]'`
2. Run the tests: `python -m pytest tests -v` (host-contract tests skip unless `HERMES_CORE` points at a Hermes checkout).
3. Lint: `ruff check .`
4. Package check: `python scripts/package.py`
5. Manifest checks: `hermes plugins doctor . --ci` and `hermes plugins validate .`

## Tests to add for behavior changes

Add tests for denied unknown, stale and invalid states, config read errors, allowed exact provider behavior, reset-window handling, and each supported API mode. Host-dependent tests belong in `tests/test_host_contract.py` and must skip cleanly without `HERMES_CORE`.

## Pull requests

- One change per pull request, with the reasoning in the description. Small pull requests get reviewed; large ones sit.
- CI must be green. It runs the offline suite on Linux, macOS and Windows for Python 3.10 and 3.12, the host-contract suite against a pinned Hermes commit, ruff, and the hygiene checks (whitespace, declared encodings, no personal or machine-specific paths).
- Behavior changes need tests and a `CHANGELOG.md` entry in the same pull request. Explain any new privilege or limitation in the README.
- Your branch is squashed on merge, so intermediate commits are fine.
- Reviews are best-effort by one person, usually within a few days. If a pull request has been quiet for a couple of weeks, a polite nudge is welcome.
- Describe what you verified and how. "Tests pass" without saying which ones is not evidence.

## Releases

- **Versioning:** SemVer. Pre-1.0, so the minor number carries features and the patch number fixes; a behavior change can land in either, which is what the CHANGELOG is for.
- **What a release is:** a tag (`vX.Y.Z`) plus a GitHub Release whose zip the release workflow builds after the suite passes on that tag. Nothing is released from an uncommitted tree, and the CHANGELOG entry is written before the tag, not after.
- **Cadence:** no fixed schedule. Fixes ship when they are ready, features when they are done. Quiet weeks are normal.
- **Catalog pin:** the Hermes plugin catalog pins an exact commit SHA. Moving that pin is a pull request against `NousResearch/hermes-agent` updating `plugin-catalog/usage-guard.yaml`, which a Hermes maintainer reviews — so catalog users only receive reviewed code.

## Governance

The maintainer decides what lands and aims to answer every issue at least once, even when the answer is no. Sustained contributors may be invited to review. Security reports are handled privately first, and disclosed in the release notes once a fix ships.

License for contributions: MIT (see [LICENSE](LICENSE)).
