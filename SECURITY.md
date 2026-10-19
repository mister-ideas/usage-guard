# Security Policy

## Reporting

Report vulnerabilities privately via GitHub security advisories on this repository (Security → Report a vulnerability). Do not open public issues for security reports.

## Supported versions

Only the latest release receives fixes. The Hermes catalog pins an exact commit, so a fix reaches catalog users through a reviewed pin bump.

## What to expect

This is a single-maintainer project: an acknowledgement usually within a few days, and a fix or a written assessment as soon as one is ready. Reports are disclosed in the release notes once a fix ships, with credit if you want it.

## Scope and design stance

- This plugin **denies by default**: stale, missing, malformed or unconfirmable usage evidence blocks the provider call. `fail_open: true` is an explicit availability-over-protection decision and must never be the silent default.
- The plugin reads no credentials, stores no tokens, and performs no network calls of its own: usage fetches go through Hermes core's `agent.account_usage.fetch_account_usage`, which resolves credentials inside the host.
- The plugin extends Hermes only through public plugin surfaces (`llm_execution` middleware). It never patches, wraps or rebinds Hermes core.
- Known limitation: auxiliary calls (compression, vision, background review) bypass `llm_execution` middleware on stock upstream Hermes and are not gated there. See README "Auxiliary calls on stock Hermes".

## Data handling

The cache under `$HERMES_HOME/plugin-data/usage-guard/` stores usage windows, timestamps, plan labels and failure reasons only — never credentials, tokens or raw provider payloads.
