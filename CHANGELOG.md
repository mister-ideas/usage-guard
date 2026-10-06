# Changelog

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
