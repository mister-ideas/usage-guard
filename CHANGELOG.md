# Changelog

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
