# usage-guard

Author: **mister-ideas**. MIT-licensed Hermes native plugin.

## Policy

Registers `llm_execution` middleware, with an independent cache over `agent.account_usage.fetch_account_usage`. It depends on nothing beyond Hermes itself.

**The only blocking point is a subscription usage window at/over the threshold.** With `threshold_percent=100`, requests pass while any subscription allowance remains (0% left = blocked) and are denied the moment a window is fully used. No account-level extra-usage or credit state is read or enforced; the plugin never touches account or billing settings. MoA turns are gated through the running preset: every enabled reference slot and the aggregator whose provider is in `provider_keys` is checked, and the turn is refused when any of them is exhausted.

Defaults: guard `anthropic` and `openai-codex` (stock providers); refresh on a 60-second freshness TTL with a 30-second per-source floor (20-second bounded wait on the stale path); deny at the threshold; `fail_open=false`.

## Freshness on long runs

Usage limits are pulled as often as the endpoint tolerates, so decisions rest on the freshest evidence the guard can get:

- The endpoint is read on a freshness TTL (`max_cache_age_s`), never per request: every guarded ingress shares one cache, and concurrent ones collapse into a single read.
- On the stale path the guard waits up to `poll_wait_timeout_s` for a synchronous refresh before deciding; stale/missing/malformed evidence denies (`fail_open=false`). With the usage endpoint unresponsive, a guarded call can stall up to this long before denying.
- Simultaneous refreshes for the same provider are coalesced (one in-flight poll; waiters share its result).
- If the usage endpoint answers HTTP 429, the guard records a bounded backoff (`Retry-After`, capped at 15 minutes and growing with repeated failures) and keeps deciding on the last good snapshot for up to `stale_grace_s` — a rate limit alone never refuses a request.
- A past `reset_at` is not treated as replenishment without fresh confirmation.

These layers mean a long run that exhausts its window mid-run is stopped at the next guarded ingress on fresh data, not on a stale reading. **An in-flight request cannot be recalled**: a request that starts inside the allowance and spills to paid usage within the same provider-side request is billed before the next snapshot can observe it; the guard stops the *next* request.

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

## What stock Hermes can and cannot gate

Gated (the `llm_execution` middleware is the only stock enforcement point):

- Main-loop dispatches for any provider listed in `provider_keys`.
- MoA turns (`provider="moa"`): the running preset is resolved through core's `normalize_moa_config`, and every **enabled reference slot** and the **aggregator** whose provider is in `provider_keys` is checked; the turn is refused when any of them is exhausted. A preset that cannot be resolved is refused unless `fail_open` is true.

Not gated on stock Hermes (observer-only upstream hooks; this plugin does not patch core):

- Auxiliary calls: compression, vision, background review, titles — and the **MoA advisor fan-out**, which runs as an auxiliary call during request assembly, *before* the middleware.
- Consequence: a MoA turn refused because a guarded slot is exhausted has already dispatched that turn's advisor fan-out. The refusal stops the aggregator and every later iteration; switch `/model` to stop advisor spend.

On Hermes builds that route auxiliary attempts through the middleware (a host-side integration), auxiliary requests are enforced the same way; auxiliary consumers reject the `usage_guard_blocked` marker rather than use it as a compression/vision result.

## Usage sources and scale

**Sources.** Every usage number comes from Hermes itself, through the documented `agent.account_usage.fetch_account_usage`, for every source including Anthropic. The core fetcher resolves each provider's stored sign-in and attaches it, so the plugin never holds a credential and never calls a usage endpoint itself. Nothing outside Hermes is imported, read or required; the guard's only cache is its own, in the plugin data dir that Hermes resolves for the active profile.

**Anthropic caveat.** Upstream's Anthropic fetcher parses the OAuth usage payload with `fraction=True`, which multiplies any `utilization` ≤ 1 by 100 — and that payload's `utilization` is a percentage scale (its own `limits[]` reports the same windows as `percent`). A healthy sub-1% window therefore arrives inflated, and the guard can only report what the core reports: at `threshold_percent=100` a session reading exactly 1% becomes 100% and blocks. Repairing that belongs upstream; a plugin that read the endpoint itself would have to hold the user's token, which the catalog forbids. Whenever the host does provide a raw payload, the guard ingests every published window at its stated percentage.

**Scale.** Window values are percentages (0-100), per the host's `AccountUsageWindow.used_percent` contract, and the guard denies on a value outside that range instead of comparing it. A source that reports fractions (0-1) must be declared in `source_scales` — otherwise a full window would read as 1% used and never block. An undeclared source whose values are all ≤ 1 is logged once with that fix.

## Rate limiting

A usage endpoint that rate-limits or fails must not become a refusal, because that would make the guard the outage. Three rules:

- **A failed refresh never erases evidence.** The last good windows and their timestamp are carried forward, so a 429 can never leave the guard with "no data".
- **A backoff never refuses a request by itself.** While the endpoint is rate-limiting, the guard keeps deciding on the last snapshot it read for up to `stale_grace_s` (default 900s), and the exhaustion interlock applies to that snapshot unchanged — a genuinely exhausted window still blocks. Beyond the grace window, the normal stale-data policy applies.
- **The request rate is capped.** `min_fetch_interval_s` (default 30s) is a hard floor between two network reads of one source, enforced across processes through the cache, so a burst of guarded ingresses (main turn, auxiliary calls, MoA slots, subagents) can never become a burst of endpoint requests. Backoff honours `Retry-After`, grows exponentially with consecutive failures, and carries jitter so the desktop backend and the gateway do not retry in lockstep.

## Refresh cadence per provider

Providers differ by orders of magnitude, so the floor between two reads is declared per source (`source_min_intervals`). The effective cadence is `max(max_cache_age_s, that floor)`, and the floor is claimed atomically under the cache's file lock — so any number of concurrent sessions, in one process or several, share **one read per interval per source**.

| Source | Smallest safe interval | Why |
|---|---|---|
| `anthropic` | **180s** | The OAuth usage endpoint has a small hourly budget per account that Claude Code itself and every other usage tool on the machine draw from. Pollers reading it every 30-60s are documented to hit a persistent 429 loop — and to starve Claude Code's own usage panel. |
| `openai-codex` | **180s** | Same class of undocumented backend budget; the Codex TUI's own poller is known to hit 429 there. |
| `openrouter` | **30s** | Publishes its limits (20 requests/minute on the free tier, scaling with credit balance). |
| anything else | `min_fetch_interval_s` (30s) | Undeclared sources fall back to the global floor. |

Lowering a floor below these values is allowed but self-defeating: the endpoint answers with a 429, the guard backs off exponentially (up to 15 minutes) and serves the last good snapshot, so the *effective* refresh rate ends up worse than the floor would have given. Several concurrent sessions never multiply the request rate — they share the cache and the atomic claim.

## Configuration

Use Hermes' supported settings CLI/UI. Keys are under `plugins.entries.usage-guard.settings`:

| Key | Default | Meaning |
|---|---|---|
| `provider_keys` | `["anthropic", "openai-codex"]` | Provider IDs gated on usage data; add custom provider plugin IDs here |
| `provider_sources` | `{"anthropic": "anthropic", "openai-codex": "openai-codex"}` | provider-id → data-source key; unmapped providers use their own ID |
| `threshold_percent` | `100.0` | Block when ANY window's used_percent >= this value (default blocks only at 0% left) |
| `max_cache_age_s` | `60.0` | Maximum snapshot age |
| `poll_wait_timeout_s` | `20.0` | Bounded refresh wait on the stale path |
| `fail_open` | `false` | Keep false for safety |
| `source_scales` | `{"anthropic": "percent", "openai-codex": "percent", "openrouter": "percent"}` | data-source key → value scale: `percent` (0-100) or `fraction` (0-1, scaled ×100 at decision time) |
| `stale_grace_s` | `900` | While a refresh is failing or rate-limited, keep deciding on the last good snapshot for up to this long instead of refusing. `0` restores the strict stale policy |
| `min_fetch_interval_s` | `30` | Hard floor between two network reads of one source, enforced across processes. Prevents the usage endpoint from rate-limiting the guard. `0` disables |

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

The account-usage data comes from core's documented `agent.account_usage.fetch_account_usage` for every source, Anthropic included; this plugin keeps its own cache under `$HERMES_HOME/plugin-data/usage-guard/` and stores only windows, timestamps, plan, and failure reasons — never credentials or raw payloads. Reads and merges are serialized with an advisory file lock (POSIX `flock` / Windows `locking`) so the desktop backend and a gateway sharing `HERMES_HOME` cannot lose each other's records.

**Disclosures**

- Network: the plugin makes **no** usage request of its own. Each refresh calls `agent.account_usage.fetch_account_usage`, which performs the authenticated read through Hermes (the Anthropic OAuth usage endpoint and the Codex `wham/usage` endpoint on `chatgpt.com`, both **undocumented upstream** and able to change). A changed or failing source fails closed (denials), never open.
- Credential binding: the `llm_execution` middleware payload does not attest which credential/account a request will use, so the guard polls the account the core resolver returns and cannot prove it matches the request. With credential pools or per-session overrides, treat the reading as account-level evidence, not request-level.
- Anthropic windows on stock Hermes: upstream's parser scales any `utilization` ≤ 1 by 100 while the payload is a percentage scale, so a sub-1% window arrives inflated (see the caveat above). The guard reads what the core reports and cannot repair it through the public API.
- Not a billing guarantee: the plugin cannot revoke in-flight requests, govern other applications or direct CLI launches, enforce policy while unloaded, or predict the next request's cost from a quota percentage. Native list-price equivalent estimates are not actual subscription overage.
- No usage surface: a provider whose account has no subscription usage to read — an Anthropic API-key account, whose limits the OAuth usage endpoint does not publish — is passed through with one warning naming the source, rather than refused. The guard's own failure reasons (`fetch-error`, `no-data`, the `malformed-*` set) keep the fail-closed policy.
- Durable state: the cache is `<data dir>/usage-guard/usage.json` in the per-plugin data root that Hermes resolves for the active profile, so profiles on one machine do not share usage evidence. It holds windows, timestamps, plan and failure reasons only — never credentials or raw payloads.

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
