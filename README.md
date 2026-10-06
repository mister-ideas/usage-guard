# usage-guard

Author: **mister-ideas**. MIT-licensed Hermes native plugin.

## Policy

Registers `llm_execution` middleware, with an independent cache over `agent.account_usage.fetch_account_usage`. It never imports the quota plugin.

**The only blocking point is a subscription usage window at/over the threshold.** With `threshold_percent=100`, requests pass while any subscription allowance remains (0% left = blocked) and are denied the moment a window is fully used. No account-level extra-usage or credit state is read or enforced; the plugin never touches account or billing settings.

Defaults: guard `anthropic` and `openai-codex` (stock providers); refresh at every ingress (20-second bounded wait); deny at the threshold; maximum snapshot age 60 seconds; `fail_open=false`.

## Freshness on long runs

Usage limits are pulled as often as needed so decisions never rely on late information:

- `refresh_every_request=true` (default) fetches a new usage snapshot at every guarded ingress; a refresh that cannot be confirmed denies even if an older snapshot is still recent.
- On the stale path the guard waits up to `poll_wait_timeout_s` for a synchronous refresh before deciding; stale/missing/malformed evidence denies (`fail_open=false`). With the usage endpoint unresponsive, a guarded call can stall up to this long before denying.
- Simultaneous refreshes for the same provider are coalesced (one in-flight poll; waiters share its result).
- If the usage endpoint answers HTTP 429, the guard records a bounded backoff (`Retry-After`, capped at 10 minutes) and denies with a clear notice until it expires — stale evidence is never reused to allow a call.
- A past `reset_at` is not treated as replenishment without fresh confirmation.

These layers mean a long run that exhausts its window mid-run is stopped at the next guarded ingress on fresh data, not on a stale 60-minute-old reading. **An in-flight request cannot be recalled**: a request that starts inside the allowance and spills to paid usage within the same provider-side request is billed before the next snapshot can observe it; the guard stops the *next* request.

## Install

From the Hermes catalog (after publication):

```
hermes plugins install usage-guard
```

Manual, from a clone of this repo:

```
hermes plugins install /path/to/usage-guard
```

Uninstall:

```
hermes plugins uninstall usage-guard
```

## Example refusal notice

```
⏸️ Usage guard blocked this request: the anthropic usage data source reports an
exhausted window for provider anthropic (Pro). Session: 100% used, resets
2026-10-06 21:47 CET — the request was not sent to the provider. Switch /model
to another provider or wait for the reset.
```

Blocked responses carry `usage_guard_blocked=True` so callers can distinguish a policy refusal from a provider answer.

## Auxiliary calls on stock Hermes

**Auxiliary calls (compression, vision, background review) are not gated by this plugin on stock upstream Hermes**: they bypass `llm_execution` middleware there. This plugin uses public plugin surfaces only and does not patch or rebind Hermes core. On Hermes builds that route auxiliary attempts through the middleware (a host-side integration), auxiliary requests are enforced the same way; auxiliary consumers reject the `usage_guard_blocked` marker rather than use it as a compression/vision result.

## Configuration

Use Hermes' supported settings CLI/UI. Keys are under `plugins.entries.usage-guard.settings`:

| Key | Default | Meaning |
|---|---|---|
| `provider_keys` | `["anthropic", "openai-codex"]` | Provider IDs gated on usage data; add custom provider plugin IDs here |
| `provider_sources` | `{"anthropic": "anthropic", "openai-codex": "openai-codex"}` | provider-id → data-source key; unmapped providers use their own ID |
| `threshold_percent` | `100.0` | Block when ANY window's used_percent >= this value (default blocks only at 0% left) |
| `max_cache_age_s` | `60.0` | Maximum snapshot age |
| `poll_wait_timeout_s` | `20.0` | Bounded refresh wait on the stale path |
| `refresh_every_request` | `true` | Require new evidence per guarded ingress |
| `fail_open` | `false` | Keep false for safety |

Custom provider plugins are gated by adding their IDs, e.g.:

```yaml
plugins:
  entries:
    usage-guard:
      settings:
        provider_keys: ["anthropic", "openai-codex", "my-custom-provider"]
        provider_sources:
          anthropic: anthropic
          openai-codex: openai-codex
          my-custom-provider: anthropic
```

The Anthropic source resolves the core Claude OAuth credential. A custom Claude config directory can refer to a different account; do not treat that as matching evidence.

## Provenance and limits

The account-usage data comes from core's `agent.account_usage.fetch_account_usage`; this plugin keeps its own cache under `$HERMES_HOME/plugin-data/usage-guard/` and stores only windows, timestamps, plan, and failure reasons — never credentials or raw payloads. Reads and merges are serialized with an advisory file lock (POSIX `flock` / Windows `locking`) so the desktop backend and a gateway sharing `HERMES_HOME` cannot lose each other's records.

**Disclosures**

- Network: one authenticated usage GET per refresh — `https://api.anthropic.com/api/oauth/usage` (Anthropic OAuth) and the Codex `wham/usage` endpoint on `chatgpt.com`. Both are **undocumented endpoints** and can change; a change fails closed (denials), never open. No tokens are read, logged or stored by this plugin; core fetchers perform the authenticated reads.
- Credential binding: the `llm_execution` middleware payload does not attest which credential/account a request will use, so the guard polls the account the core resolver returns and cannot prove it matches the request. With credential pools or per-session overrides, treat the reading as account-level evidence, not request-level.
- Raw usage windows on stock Hermes: upstream core attaches the raw usage payload to Codex snapshots but not yet to Anthropic ones. On stock core, Anthropic gating covers the classic windows (session/weekly/model weeks); the additional raw buckets (`seven_day_oauth_apps`, `seven_day_overage_included`, `limits[]`) are ingested whenever the core provides `raw` — the guard logs a one-time warning when it does not. Fail-closed either way.
- Not a billing guarantee: the plugin cannot revoke in-flight requests, govern other applications or direct CLI launches, enforce policy while unloaded, or predict the next request's cost from a quota percentage. Native list-price equivalent estimates are not actual subscription overage.

## Development

```
python -m pip install -e '.[dev]'
python -m pytest tests -v          # offline suite (host-contract tests skip)
ruff check .
python scripts/package.py          # dist/usage-guard-<version>.zip
```

Host-contract tests run against a real Hermes tree: point `HERMES_CORE` at a checkout (CI pins one):

```
HERMES_CORE=/path/to/hermes-agent python -m pytest tests/test_host_contract.py -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Changelog: [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
