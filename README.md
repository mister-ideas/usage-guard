# usage-guard for Hermes Agent

![usage-guard: a marble Hermes halts before a full usage gauge. Stop your Hermes agent at the subscription wall.](docs/banner.jpg)

[![CI](https://github.com/mister-ideas/usage-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/mister-ideas/usage-guard/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/mister-ideas/usage-guard)](https://github.com/mister-ideas/usage-guard/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Author: **mister-ideas**. MIT-licensed Hermes native plugin.

Keeps a Hermes agent inside its subscription allowance: when a provider's usage window is exhausted, the request is refused instead of sent — so an account that allows paid extra usage stops at the wall rather than spending overage.

## Why this exists

Subscription plans now bill **extra usage** — paid overage once a plan window is exhausted — and on many accounts it is already switched on, whether an administrator enabled it or the plan ships with it. An unattended agent is exactly the workload that spends it: it keeps working at 100% used, and the first request past the wall is billed.

This plugin makes Hermes stop at the wall instead. When a subscription window reads fully used, the provider dispatch is refused, so the agent reports the limit rather than spending beyond it. It reads the subscription usage Hermes already displays; it never touches your account, billing or credit settings, and it never holds your credentials.

Who it is for: anyone on a subscription whose account allows extra usage and who wants the agent confined to the subscription allowance — including a shared or company plan, where one agent should not consume the organization's paid credits.

**What it is not.** It cannot guarantee zero extra usage: a request that starts inside the allowance can still spill into paid usage on the provider's side, and the guard stops the next one (see [Provenance and limits](#privacy-and-disclosure)). If you need a hard guarantee, disable extra usage on the account itself — this plugin is the in-Hermes layer that keeps the agent from crossing the line on its own.

## Requirements

- Hermes `>=0.21.6` (the `llm_execution` middleware and the account-usage fetcher).
- No Python dependencies, no API keys, no account changes.
- Any platform Hermes runs on.

## Install

From the Hermes catalog — usage-guard is listed there ([entry](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/usage-guard.yaml), added in [NousResearch/hermes-agent#139993](https://github.com/NousResearch/hermes-agent/pull/139993)):

```sh
hermes plugins install usage-guard
```

Straight from this repository — `hermes plugins install` accepts a Git URL or `owner/repo`, and `--ref` pins an exact reviewed commit:

```sh
hermes plugins install mister-ideas/usage-guard
```

Then make sure it is enabled and **restart Hermes**, because middleware is bound when the backend process starts:

```sh
hermes plugins enable usage-guard
```

Uninstall:

```sh
hermes plugins remove usage-guard
```

## Updating

```sh
hermes plugins check-updates          # read-only: is a newer version available?
hermes plugins update usage-guard
```

Restart Hermes afterwards: middleware is bound when the backend process starts. The catalog pins an exact reviewed commit, so an update reaches you only after a maintainer merges the pin bump.

## What happens on first install

- It gates `anthropic` and `openai-codex` — the stock subscription providers — and nothing else. A provider that is not listed in `provider_keys` passes through untouched, so installing it changes nothing if you don't use those.
- Nothing is blocked until a window actually reads fully used. The only blocking point is that window (default `threshold_percent: 100`, i.e. 0% left).
- It reads usage through Hermes's own fetcher and keeps one cache file in the plugin data dir. It holds no credentials and makes no usage request of its own.
- Confirm it is loaded: `hermes plugins list` shows it enabled, and a refusal names the plugin in the log (`plugins.usage-guard`).
- Running a provider plugin under its own ID (for example a Claude-subscription provider)? Add that ID to `provider_keys` and map it to a data source — see [Configuration](#configuration).

## Configuration

Use Hermes' supported settings CLI/UI. Keys live under `plugins.entries.usage-guard.settings`:

| Key | Default | Meaning |
|---|---|---|
| `provider_keys` | `["anthropic", "openai-codex"]` | Provider IDs gated on usage data; add custom provider plugin IDs here |
| `provider_sources` | `{"anthropic": "anthropic", "openai-codex": "openai-codex"}` | provider-id → data-source key; unmapped providers use their own ID |
| `threshold_percent` | `100.0` | Block when ANY window's used_percent >= this value (default blocks only at 0% left) |
| `max_cache_age_s` | `60.0` | Maximum snapshot age |
| `poll_wait_timeout_s` | `20.0` | Bounded refresh wait on the stale path |
| `fail_open` | `false` | Allow requests when evidence is stale, missing or malformed. Keep `false` for safety |
| `source_scales` | `{"anthropic": "percent", "openai-codex": "percent", "openrouter": "percent"}` | data-source key → value scale: `percent` (0-100) or `fraction` (0-1, scaled ×100 at decision time) |
| `stale_grace_s` | `900` | While a refresh is failing or rate-limited, keep deciding on the last good snapshot for up to this long instead of refusing. `0` restores the strict stale policy |
| `min_fetch_interval_s` | `30` | Hard floor between two network reads of one source, enforced across processes. `0` disables |
| `source_min_intervals` | `{"anthropic": 180, "openai-codex": 180, "openrouter": 30}` | Per-source floors; see [Refresh cadence per provider](#refresh-cadence-per-provider) |
| `warn_percents` | `[80, 90]` | Usage levels (percent used) at which one short note is added to the next turn's context, so the agent can wind down before the refusal. `[]` disables |
| `near_wall_percent` | `90.0` | From this level up (and below `threshold_percent`), `max_tokens_near_wall` applies |
| `max_tokens_near_wall` | `0` | Cap on each request's output budget near the wall, to limit how far an in-flight request can spill. `0` disables (default: a cap can truncate answers) |

Want a safety margin instead of hitting the wall? Set `threshold_percent: 97`. The default stays 100 so the only blocking point is the subscription limit itself.

Custom provider plugins are gated by adding their IDs, for example:

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

## What stock Hermes can and cannot gate

Gated (the `llm_execution` middleware is the only stock enforcement point):

- Main-loop dispatches for any provider listed in `provider_keys`.
- MoA turns (`provider="moa"`): the guard resolves the running preset from the live `moa` config itself, and every **enabled reference slot** and the **aggregator** whose provider is in `provider_keys` is checked; the turn is refused when any of them is exhausted. A preset that cannot be resolved is refused unless `fail_open` is true.

Not gated on stock Hermes (observer-only upstream hooks; this plugin does not patch core):

- Auxiliary calls: compression, vision, background review, titles — and the **MoA advisor fan-out**, which runs as an auxiliary call during request assembly, *before* the middleware.
- Consequence: a MoA turn refused because a guarded slot is exhausted has already dispatched that turn's advisor fan-out. The refusal stops the aggregator and every later iteration; switch `/model` to stop advisor spend.

To keep auxiliary work off a guarded provider today, point the task at another provider with the stock `auxiliary.<task>` settings (for example `auxiliary.compression`, `auxiliary.title_generation`). Auxiliary calls that run inside a turn are followed by a main-loop request, which the guard can still refuse; the uncovered case is auxiliary work with no turn (cron, idle gateway work).

Closing the gap needs a Hermes change: an opt-in `auxiliary_execution` middleware kind, proposed in [NousResearch/hermes-agent#140694](https://github.com/NousResearch/hermes-agent/pull/140694) (open, not in any release yet). On Hermes builds that route auxiliary attempts through the middleware (a host-side integration), auxiliary requests are enforced the same way; auxiliary consumers reject the `usage_guard_blocked` marker rather than use it as a compression/vision result.

## Near the wall

Both features read the cache only: they never call the usage endpoint and never act on stale evidence.

- **Early notice (`pre_llm_call`).** When a window first reaches a level in `warn_percents`, one short note is added to that turn's context. It names the provider, the percentage and the reset time. Each level fires once; it re-arms after a reset. The provider comes from the session's previous request, so the first turn of a session stays silent.
- **Output cap (`llm_request`, opt-in).** With `max_tokens_near_wall` set, a request to a guarded provider that is inside `[near_wall_percent, threshold_percent)` has its existing `max_tokens` / `max_completion_tokens` / `max_output_tokens` lowered to the cap. It never adds a key and never raises a budget. Shaping never refuses anything: the `llm_execution` gate stays the only enforcer.

Neither can recall a request already in flight; they make the remaining headroom go further.

## Long unattended runs (`/goal`, cron, kanban)

The guard checks every main-loop request, including each request inside a long tool-calling turn and subagent requests, not once per user prompt. The refusal text now says it is a usage-limit stop and that autonomous loops should pause until the reset. Hermes itself does not special-case the refusal marker (`usage_guard_blocked`), so the host's loop controls still decide what happens next. Keep a turn budget on unattended loops (`/goal` has `max_turns`).

## How this differs from Hermes core's own usage work

Two open Hermes pull requests add advisory usage handling in core: [#47935](https://github.com/NousResearch/hermes-agent/pull/47935) (an opt-in `usage_guard` config for Codex) and [#60762](https://github.com/NousResearch/hermes-agent/pull/60762) (a fail-open guard before `/goal` continuation). Neither is in a release as far as this README knows. usage-guard is the hard, fail-closed layer: it refuses the provider dispatch itself and denies when evidence is missing or stale. Core's versions warn or pause; this one stops the call.

## Example refusal notice

```text
⏸️ Usage guard blocked this request: the anthropic usage data source reports an
exhausted window for provider anthropic (Pro). Session: 100% used, resets
2026-10-06 21:47 CET — the request was not sent to the provider. Switch /model
to another provider or wait for the reset. This is a usage-limit stop, not a task
result: autonomous loops (/goal, cron) should pause until the reset instead of
retrying.
```

Blocked responses carry `usage_guard_blocked=True` so callers can distinguish a policy refusal from a provider answer.

## Usage sources and scale

**Sources.** Every usage number comes from Hermes itself, through the documented `agent.account_usage.fetch_account_usage`, for every source including Anthropic. The core fetcher resolves each provider's stored sign-in and attaches it, so the plugin never holds a credential and never calls a usage endpoint itself. Nothing outside Hermes is imported, read or required; the guard's only cache is its own, in the plugin data dir that Hermes resolves for the active profile.

**Scale.** Window values are percentages (0-100), per the host's `AccountUsageWindow.used_percent` contract, and the guard denies on a value outside that range instead of comparing it. A source that reports fractions (0-1) must be declared in `source_scales` — otherwise a full window would read as 1% used and never block. An undeclared source whose values are all ≤ 1 is logged once with that fix.

## Freshness on long runs

Usage limits are pulled as often as the endpoint tolerates, so decisions rest on the freshest evidence the guard can get:

- The endpoint is read on a freshness TTL (`max_cache_age_s`), never per request: every guarded ingress shares one cache, and concurrent ones collapse into a single read.
- On the stale path the guard waits up to `poll_wait_timeout_s` for a synchronous refresh before deciding; stale/missing/malformed evidence denies (`fail_open=false`). With the usage endpoint unresponsive, a guarded call can stall up to this long before denying.
- Simultaneous refreshes for the same provider are coalesced (one in-flight poll; waiters share its result).
- If the usage endpoint answers HTTP 429, the guard records a bounded backoff (`Retry-After`, capped at 15 minutes and growing with repeated failures) and keeps deciding on the last good snapshot for up to `stale_grace_s` — a rate limit alone never refuses a request.
- A past `reset_at` is not treated as replenishment without fresh confirmation.

These layers mean a long run that exhausts its window mid-run is stopped at the next guarded ingress on fresh data, not on a stale reading. **An in-flight request cannot be recalled**: a request that starts inside the allowance and spills to paid usage within the same provider-side request is billed before the next snapshot can observe it; the guard stops the *next* request.

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

## Privacy and disclosure

The account-usage data comes from core's documented `agent.account_usage.fetch_account_usage` for every source, Anthropic included. The guard's own cache is `usage.json` in `plugin-data/usage-guard/`, in the per-plugin data root Hermes resolves for the active profile, and it stores only windows, timestamps, plan and failure reasons — never credentials or raw payloads. Reads and merges are serialized with an advisory file lock (POSIX `flock` / Windows `locking`) so the desktop backend and a gateway sharing one Hermes home cannot lose each other's records.

- Network: the plugin makes **no** usage request of its own. Each refresh calls `agent.account_usage.fetch_account_usage`, which performs the authenticated read through Hermes (the Anthropic OAuth usage endpoint and the Codex `wham/usage` endpoint on `chatgpt.com`, both **undocumented upstream** and able to change). A changed or failing source fails closed (denials), never open.
- Credential binding: the `llm_execution` middleware payload does not attest which credential/account a request will use, so the guard polls the account the core resolver returns and cannot prove it matches the request. With credential pools or per-session overrides, treat the reading as account-level evidence, not request-level.
- Anthropic windows on stock Hermes: upstream's parser scales any `utilization` ≤ 1 by 100 while the payload is a percentage scale, so a sub-1% window arrives inflated (see [Troubleshooting](#troubleshooting)). The guard reads what the core reports and cannot repair it through the public API.
- Not a billing guarantee: the plugin cannot revoke in-flight requests, govern other applications or direct CLI launches, enforce policy while unloaded, or predict the next request's cost from a usage percentage. Native list-price equivalent estimates are not actual subscription overage.
- No usage surface: a provider whose account has no subscription usage to read — an Anthropic API-key account, whose limits the OAuth usage endpoint does not publish — is passed through with one warning naming the source, rather than refused. The guard's own failure reasons (`fetch-error`, `no-data`, the `malformed-*` set) keep the fail-closed policy.
- Durable state: two profiles on one machine keep separate usage evidence, because the cache path is resolved per profile. It holds windows, timestamps, plan and failure reasons only — never credentials or raw payloads.

## Troubleshooting

| Symptom | What is happening | What to do |
|---|---|---|
| Blocked at a few percent | On stock Hermes, upstream's Anthropic parser scales any `utilization` ≤ 1 by 100, so a window at 0–1% can arrive as 100%. It hits every account right after a reset. | Nothing to fix here: it is [upstream's parser](https://github.com/NousResearch/hermes-agent/issues/58226), fixed by [PR #139916](https://github.com/NousResearch/hermes-agent/pull/139916). Until a release carries it: wait for the reset, or drop the provider from `provider_keys` if you accept it being ungated. |
| Blocked although usage looks fine | The window the notice names is at/over the threshold *in the snapshot the guard read*. The notice prints the label, the value and the reset time. | Read the notice: it names the window. Compare with `hermes usage`. A stale reset is never treated as replenishment. |
| Blocked, notice says the configuration is malformed | A setting has the wrong shape, and a broken policy denies by design rather than silently disabling the guard. | Fix `plugins.entries.usage-guard.settings` (or remove the plugin). The log line names the key. |
| Blocked with "usable allowance evidence is missing" | No snapshot within `stale_grace_s` (900s) — the source is failing or unreadable. | Check `grep plugins.usage-guard ~/.hermes/logs/agent.log*`. If you prefer availability over protection, set `fail_open: true` knowingly. |
| It never blocks | The provider may not be listed in `provider_keys`, the window may genuinely be under the threshold, or the call may not be gated at all. | Check the log: the guard prints `usage-guard: gating providers [...]` on its first guarded dispatch, and names each provider that passes through because it is not gated. See also [What stock Hermes can and cannot gate](#what-stock-hermes-can-and-cannot-gate). |
| "reports no account usage to guard" warning | The account has no subscription usage surface — an Anthropic API-key account publishes no limits on the OAuth usage endpoint. | Expected: the provider passes through rather than being blocked. Remove it from `provider_keys` to silence the warning. |
| "usage endpoint rate-limiting" messages | The usage endpoint answered 429. The guard backs off (honouring `Retry-After`) and keeps deciding on the last good snapshot. | Nothing — a rate limit never refuses a request. Raising a floor or refreshing harder only deepens the backoff. |
| I need to unblock right now | — | Switch `/model` to another provider, wait for the reset, remove the provider from `provider_keys`, or `hermes plugins remove usage-guard`. |

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests -v          # offline suite (host-contract tests skip)
ruff check .
hermes plugins validate . --install-deps
python scripts/package.py          # dist/usage-guard-<version>.zip
```

Host-contract tests run against a real Hermes tree: point `HERMES_CORE` at a checkout (CI pins one):

```sh
HERMES_CORE=/path/to/hermes-agent python -m pytest tests/test_host_contract.py -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Changelog: [CHANGELOG.md](CHANGELOG.md).

## Support, issues and releases

- Bugs and feature requests: the [issue forms](https://github.com/mister-ideas/usage-guard/issues/new/choose). The bug form asks for the refusal notice, your versions and the `plugins.usage-guard` log lines — that evidence is what makes a report reproducible.
- Questions and ideas: [Discussions](https://github.com/mister-ideas/usage-guard/discussions).
- Security: [SECURITY.md](SECURITY.md) — reported privately, never in a public issue.
- Community expectations: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) — what is in and out of scope, and how releases are cut.

Releases are SemVer tags with a GitHub Release whose zip the release workflow builds only after the suite passes. There is no fixed cadence: fixes ship when they are ready. The Hermes plugin catalog pins an exact commit SHA, and moving that pin is a reviewed pull request against `NousResearch/hermes-agent`, so catalog users only ever receive reviewed code.

## License

MIT — see [LICENSE](LICENSE).
