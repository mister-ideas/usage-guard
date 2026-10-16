# Changelog

## 0.1.5 — 2026-10-16

Refresh cadence is per provider, at the smallest safe interval each one allows.

### Guard behavior

- `source_min_intervals` (new): the smallest safe seconds between two reads of a source. Providers differ by orders of magnitude, so one global floor either starves the freshest source or hammers the most sensitive one. Defaults: `anthropic` 180, `openai-codex` 180, `openrouter` 30; an undeclared source falls back to `min_fetch_interval_s`.
- The Anthropic floor is 180s because that endpoint has a small hourly budget per account that Claude Code itself and every other usage tool on the machine draw from: pollers reading it every 30-60s are documented to hit a persistent 429 loop and to starve Claude Code's own usage panel. OpenRouter publishes a 20 requests/minute floor, so 30s is comfortable there.
- The effective cadence is `max(max_cache_age_s, per-source floor)`, and the floor is claimed atomically under the cache's file lock, so any number of concurrent sessions — one process or several — share one read per interval per source.

## 0.1.4 — 2026-10-16

Per-request refresh is gone; refreshes are rate-budgeted and shared.

### Guard behavior

- **`refresh_every_request` is removed.** Reading the usage endpoint on every guarded ingress is what caused the 429s: several ingresses (main turn, auxiliary calls, MoA slots, subagents) each produced a request. Refresh is now driven purely by a freshness TTL (`max_cache_age_s`, default 60s), bounded by `min_fetch_interval_s` (default 30s) and any rate-limit backoff.
- **Concurrent requests share one read.** The right to read a source is claimed atomically under the cache's advisory file lock, so X guarded requests at the same moment — several threads, or the desktop backend and the gateway sharing `HERMES_HOME` — collapse into a single usage-endpoint request per interval. Callers that lose the claim wait (bounded by `poll_wait_timeout_s`) for the winner's result instead of deciding on a stale record.
- **A read that could not land no longer refuses.** With the per-request confirmation check gone, the decision rests on the kept windows within `stale_grace_s`, exactly as a rate-limited read does.

## 0.1.3 — 2026-10-16

Rate limiting no longer refuses requests.

### Guard behavior

- A failed or rate-limited refresh (HTTP 429) no longer erases the last good snapshot: its windows and timestamp are carried forward, so a rate limit can never leave the guard with no evidence.
- A backoff alone never refuses a request. While the usage endpoint is rate-limiting or failing, the guard keeps deciding on the last snapshot it read, for up to `stale_grace_s` (default 900s), and the exhaustion interlock still applies to that snapshot — a genuinely exhausted window still blocks. Set `stale_grace_s: 0` for the previous strict behaviour.
- `min_fetch_interval_s` (new, default 30s): a hard floor between two network reads of one source, enforced across processes through the cache, so a burst of guarded ingresses (main turn, auxiliary calls, MoA slots, subagents) can never become a burst of usage-endpoint requests. This is what prevents the 429s rather than reacting to them.
- Backoff now honours `Retry-After`, grows exponentially with consecutive failures, is capped at 900s, and carries jitter so the desktop backend and the gateway do not retry in lockstep.
- With `refresh_every_request: true`, an unconfirmed refresh only refuses when no usable evidence remains; it no longer refuses merely because the endpoint declined to answer.

## 0.1.2 — 2026-10-16

Full scale pass over every usage value the guard ingests, and an explicit statement that the usage sources are Hermes' own providers.

### Guard behavior

- `source_scales` (new): declare a data source's value scale — `percent` (0-100, the host's `AccountUsageWindow.used_percent` contract, the default) or `fraction` (0-1, scaled ×100 at decision time). A fractional source that is not declared would read a full window as 1% and never block; an undeclared source whose values are all ≤ 1 is now logged once with the fix.
- Out-of-range values (below 0, or above 100 after scaling) are treated as malformed evidence and deny, instead of being stored and compared.
- The Anthropic source keeps its direct read, so its published percentages are never rescaled by the host's fraction heuristic.

### Provenance

- Usage sources are Hermes' own providers: the core fetcher for `openai-codex`/`openrouter` and a direct read for the Anthropic source, both using the logins Hermes already stores. No other plugin (notably not `quota`) is imported, read or required, and the guard's only cache is its own.

## 0.1.1 — 2026-10-16

Fix a false "exhausted" reading on the Anthropic source — affecting every provider mapped to it (`anthropic`, `claude-subscription-directsdk-experimental`, and any user-mapped alias).

### Guard behavior

- The host's Anthropic fetcher parses the OAuth usage payload with a fraction heuristic that multiplies any `utilization` ≤ 1 by 100. That payload is a percentage scale — its own `limits[]` array reports the same windows as `percent` (session 4, weekly_all 1) — so a healthy 1% week arrived as 100% and the guard refused a healthy account. The plugin now reads the Anthropic payload itself and never scales these values: a 1% week stays 1%.
- Reading the payload directly also brings the full published window set into the gate on stock Hermes: the classic session/weekly windows, the model-scoped weeks, and every `limits[]` entry. Previously only the host's classic windows were visible, because the host attaches no raw payload for Anthropic.
- Transport failures on that read keep the existing fail-closed policy: an HTTP 429 records a bounded backoff and the guard denies until it expires — never on stale evidence.

## 0.1.0 — 2026-10-06

First release. The guard blocks provider calls exactly when a subscription usage window reaches `threshold_percent` (default 100 = 0% left) on fresh, confirmed evidence — and never otherwise. Earlier 0.x numbers were internal development builds and were never released.

### Guard behavior

- Fail-closed on stale, missing, malformed or unconfirmable usage evidence; `refresh_every_request` (default on) fetches a new snapshot per guarded ingress and denies when the refresh cannot be confirmed.
- HTTP 429 from a usage endpoint records a bounded backoff (`Retry-After`, capped at 10 min) and denies with a notice until it expires; stale evidence is never reused to allow a call.
- Conservative scope gating: all raw windows published by the core fetchers are gated — Anthropic `five_hour`/`seven_day`/model weeks plus `seven_day_*` buckets and `limits[]` when the core provides `raw`; Codex `rate_limit`, `additional_rate_limits[]` (flat and nested) and `code_review_rate_limit`, including `allowed=false` / `limit_reached=true` server flags. An exhausted code-review window also blocks inference (the guard cannot prove the next model is exempt).
- Codex `reset_at` accepts ISO strings as well as unix seconds.
- Stock defaults gate only stock providers (`anthropic`, `openai-codex`); custom provider plugins are added via settings.
- Refusal responses carry `usage_guard_blocked=True` for `chat_completions`, `anthropic_messages` and `codex_responses`; an unrecognized api_mode still blocks.
- MoA turns (`provider="moa"` — the middleware payload for every `provider: moa` session) resolve the running preset through core's `normalize_moa_config` and gate every enabled reference slot plus the aggregator whose provider is in `provider_keys`; the turn is refused with the standard notice when any of them is exhausted. An unresolvable preset follows `fail_open` (deny by default).

### Infrastructure

- Advisory-locked, merge-based cache updates (POSIX `flock` / Windows `locking`) with unique temp names, safe across the desktop backend and a gateway sharing `HERMES_HOME`.
- Refresh confirmation via `(fetched_at, polled_at)` so a poll landing in the same second as the previous fetch still counts as confirmed.
- Portable pytest suite (offline + host-contract against a pinned Hermes revision), ruff lint, packaging smoke test, and a zero-collection guard in CI.

### Known limitations

- Auxiliary calls (compression, vision, background review) and the MoA advisor fan-out run through observer-only upstream hooks and are not gated on stock Hermes; see README "What stock Hermes can and cannot gate".
- An in-flight request cannot be recalled; the guard stops the next request on fresh data.
