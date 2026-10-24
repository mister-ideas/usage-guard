# Changelog

All notable changes to this plugin. Versions follow [SemVer](https://semver.org); each release is a `vX.Y.Z` tag with a GitHub Release.

## 0.4.0 — 2026-10-24

Clearer messages and a rewritten README. No change to what is gated, when, or how it is decided.

- **Refusals are plain language.** The chat notice now says what was refused and why, names the threshold, gives the next steps (`/model`, wait for the reset, pause autonomous runs), and states that nothing was sent. It uses markdown, so Desktop and gateways render it cleanly.
- **Unavailable readings say what is wrong.** A missing, empty or stale reading names its cause in plain words, and points at `hermes usage --provider <id>`.
- **Settings warnings tell the truth.** A malformed setting used to say "using default", while the guard in fact refused requests. Each warning now says that guarded requests are refused until the setting is fixed.
- **Log lines are consistent.** Refusals log as `usage-guard refused …`; the first guarded dispatch logs `usage-guard active: gating providers …`.
- **README rebuilt.** The top of the page now shows what the user will see, with screenshots and a short demo. The deep mechanics moved to `docs/reference.md`.
- Test updated to the new refusal text. Host-contract check updated to the current core installer API (`PythonDeclaration` no longer has `is_member`).

## 0.3.9 — 2026-10-19

A snapshot is no longer called stale before the guard is allowed to replace it. The freshness window is now `max(max_cache_age_s, the source's read floor)` — the value the README already described as the effective cadence.

With a 60s window and the 180s floor declared for `anthropic` and `openai-codex`, every dispatch landing in between found the record stale, asked for a refresh, was refused by the floor, and then refused the request for "no fresh snapshot" — while a usable snapshot sat on disk. Fail-closed is unchanged past the floor: when no refresh lands and no read failure explains it, the guard still refuses rather than deciding on evidence it cannot vouch for, and the exhaustion interlock applies to whatever record is read.

## 0.3.8 — 2026-10-19

The guard now says what it gates. It logs `usage-guard: gating providers [...]` once on its first guarded dispatch — provider list, sources, threshold, `fail_open` — and names each provider that passes through because it is not in `provider_keys`. The README's "It never blocks" row points at both lines.

An installed-but-unconfigured guard was previously indistinguishable in the logs from one that gates and finds nothing to block, so a provider missing from `provider_keys` looked like a working guard that never fired.

Version 0.3.7 was withdrawn before release: its merge commit was authored with a personal address, which this repository's identity guard rejects, and an immutable release reserves its tag name. The content is unchanged.

## 0.3.6 — 2026-10-19

Commit identity clean-up. No change to the plugin itself.

- Repository history was rewritten so every commit and tag uses the maintainer's GitHub noreply identity only; two stray addresses (an old agent identity and a personal mailbox) are gone from branches and tags. Release 0.3.4 and 0.3.5 were withdrawn with it: their tags pointed at the old history and, being immutable, could only be removed with the release. Their contents are in 0.3.6.
- CI now fails a change that introduces one of those addresses, or that authors a commit as `mister-ideas` with any other address (new `Commit identities` step in the `Hygiene` job).

## 0.3.5 — 2026-10-19

README title is now `usage-guard for Hermes Agent`, matching git-review's `git-review for Hermes Desktop`. No behaviour change in the guard.

## 0.3.4 — 2026-10-19

Repository-settings pass with git-review. No behaviour change in the guard.

- Workflows pin every action to a full commit SHA (with the tag in a comment); Dependabot keeps the pins current.
- Releases are immutable: assets and tags cannot change once published. The release workflow already creates the release and its zip in one step, so it is unaffected; its step now has the same shape as git-review's.
- `.github/CODEOWNERS`, `.gitattributes` (LF line endings, binary assets) and one shared `.gitignore` layout.
- Repository wiki and projects are off, as in git-review.

## 0.3.3 — 2026-10-19

Consistency pass with git-review: same README skeleton, same asset names, same manifest layout. No behaviour change in the guard.

- README: new **Requirements** section; the disclosures live under **Privacy and disclosure**; **Troubleshooting** follows it, before Development; the "Near the wall" heading no longer carries a version number; shell blocks are tagged `sh`; the Development block runs `hermes plugins validate`. Fixed a link that pointed at a heading that no longer exists.
- Banner is `docs/banner.jpg` (was `assets/banner.png`), social preview is `docs/social-preview.png` (was `assets/social-preview.png`).
- `plugin.yaml`: one-sentence `description`, `manifest_version: 1` and an explicit `requires_env: []`, in the same key order as git-review.

## 0.3.2 — 2026-10-19

Catalog-readiness pass, compared against the most-starred Hermes plugins. No behaviour change in the guard.

- README: the banner is a relative path, so the catalog page renders the image from the pinned tree instead of a moving `main`; new **Updating** section.
- SECURITY.md: supported versions and what to expect after a report.
- Banner and social preview redrawn in the same style as git-review: a marble Hermes halting before a full usage gauge, `USAGE-GUARD` title, `mister-ideas` signature (still 1200×600 and 1280×640).

## 0.3.1 — 2026-10-19

First-user hardening. No behaviour change in the guard itself.

- **No `pyproject.toml`.** Hermes's installer treats any plugin with a `pyproject.toml` as a Python-dependency member: a normal install asked "declares Python dependencies", and a headless `plugins install --enable` could not enable the plugin. The plugin has no runtime dependencies, so the file only carried dev tooling. Ruff and pytest settings moved to `ruff.toml` and `pytest.ini`; dev tools are in `requirements-dev.txt`; `plugin.yaml` is now the only place the version lives.
- **Banner and social-preview image** in `assets/` (2:1, as the catalog `image:` field expects); the README opens with the banner.
- **Tests.** A host-contract test asserts the installer sees no Python declaration, and an offline test keeps `pyproject.toml` and runtime `requirements*.txt` out of the plugin root.
- `.gitignore` covers egg-info and bytecode.

## 0.3.0 — 2026-10-17

Near-the-wall behaviour, all opt-out or opt-in, all cache-only (no new reads of the usage endpoint).

- **Early notice.** A `pre_llm_call` hook adds one short note to the next turn's context at each `warn_percents` level (default 80 and 90), so the agent can wind down before the refusal. New capability: `provides_hooks: [pre_llm_call]`.
- **Output cap, opt-in.** An `llm_request` middleware lowers an existing output budget to `max_tokens_near_wall` while a window is inside `[near_wall_percent, threshold_percent)`. Off by default (`0`). New capability: `provides_middleware` gains `llm_request`. It never refuses; the `llm_execution` gate stays the only enforcer.
- **Refusal wording.** The refusal now says it is a usage-limit stop and that autonomous loops should pause until the reset.
- **Tests.** The manifest test now compares `plugin.yaml` against what `register()` actually registers (catalog rule 6). New `tests/test_near_wall.py`.
- **Docs.** New "Near the wall", "Long unattended runs" and "How this differs from Hermes core" sections; the auxiliary-call section lists the stock `auxiliary.<task>` workaround; a `threshold_percent: 97` margin is documented.

Upgrade note: the catalog entry's `capabilities` block must list the new hook and middleware when the pin moves to this version.

## 0.2.8 — 2026-10-16

README pass, compared against the most-starred Hermes plugin READMEs. Adds a Requirements line, a "What happens on first install" section and a Troubleshooting table; moves Install up to the top. Fixes two wrong claims: the install command (`hermes plugins install` takes a catalog name, a Git URL or `owner/repo` — not a filesystem path) and the MoA resolver (the guard resolves the running preset itself and no longer imports core's normalizer). Documentation only.

## 0.2.7 — 2026-10-16

Say why the plugin exists. The README gains a "Why this exists" section and the catalog entry's description leads with the purpose — keeping an agent inside the subscription allowance on an account that allows paid extra usage — instead of only describing the mechanism. Documentation only.

## 0.2.6 — 2026-10-16

Fix a trailing blank line in `CODE_OF_CONDUCT.md` that failed the CI whitespace check on 0.2.5. Documentation only.

## 0.2.5 — 2026-10-16

Open-source readiness: a Contributor Covenant code of conduct, a feature-request form, issue-template configuration (blank issues off, with pointers to private security reporting and Discussions), Dependabot for the workflow actions, README badges, and a CONTRIBUTING guide that documents scope, the pull-request process and the release policy. No plugin code changed.

## 0.2.4 — 2026-10-16

Documentation and test only: the README, the changelog and the dependency test no longer name another plugin. They state the invariant instead — the guard depends on nothing beyond Hermes's own documented surfaces — and the test parses the module's imports and asserts the exact allowlist (the standard library plus `agent.account_usage`, `hermes_cli.config` and `plugins.plugin_storage`) rather than grepping for a name. No plugin behavior changed.

## 0.2.3 — 2026-10-16

Repository hygiene only: `plugin-data/` joins the other cache directories in `.gitignore`, so running the plugin with `HERMES_HOME` pointed at a clone cannot leave an untracked cache directory. No plugin code changed.

## 0.2.2 — 2026-10-16

Repository hygiene only: `.ruff_cache/` joins the other cache directories in `.gitignore`, so a local lint run cannot leave an untracked directory in a clone. No plugin code changed.

## 0.2.1 — 2026-10-16

Portability release: two assumptions about the author's own setup are gone, so the guard behaves correctly on a machine that is not a Claude-subscription desktop.

### Guard behavior

- **A provider whose account has no subscription usage to read is no longer refused.** Hermes's Anthropic fetcher reports a deliberate reason for an API-key account (the OAuth usage endpoint publishes no limits for it). That reason is not one of this plugin's own failure reasons, and with no windows to evaluate the guard used to deny every request for that provider — blocking a healthy account it has no business gating. Such a source now passes, with one warning naming it; the plugin's own reasons (`fetch-error`, `no-data`, the `malformed-*` set) keep the fail-closed policy.
- **State lives in the documented per-plugin data dir.** The cache path is resolved through `plugins.plugin_storage.plugin_data_dir`, which follows the active profile (context override → `HERMES_HOME` → platform default) instead of reading `HERMES_HOME` and falling back to `~/.hermes`. Two profiles on one machine keep separate usage evidence. A Hermes without that helper still resolves the same layout by hand.

## 0.2.0 — 2026-10-16

Catalog-compliance release: the guard now reaches Hermes only through documented surfaces.

### Guard behavior

- **The direct Anthropic usage read is removed.** The plugin no longer resolves a subscription token, no longer sends a request with a vendor client's user agent, and no longer talks to a usage endpoint itself. Every source, Anthropic included, goes through the documented `agent.account_usage.fetch_account_usage`, which resolves the provider's stored sign-in and attaches it — so no credential ever reaches plugin code (catalog rule 11).
- **The MoA preset resolver is local.** The guard no longer imports the core's private `hermes_cli.moa_config`; it validates the `moa` config itself from the host's documented semantics, and a host-contract test asserts it agrees with the core on a real sample (catalog rule 9).
- **Consequence, stated plainly:** upstream's Anthropic parser multiplies any `utilization` <= 1 by 100 while that payload's values are percentages, so a sub-1% window arrives inflated and the guard cannot repair it from the public API. At `threshold_percent=100`, a session reported as exactly 1% reads as 100% and blocks. Fixing it belongs upstream, not in a plugin.
- `requires_hermes` is now `>=0.21.6`: the first stable release that ships the `llm_execution` middleware, `fetch_account_usage` and its documented `read_only` argument.

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

- Usage sources are Hermes' own providers: the core fetcher for `openai-codex`/`openrouter` and a direct read for the Anthropic source, both using the logins Hermes already stores. Nothing outside Hermes is imported, read or required, and the guard's only cache is its own.

## 0.1.1 — 2026-10-16

Fix a false "exhausted" reading on the Anthropic source — affecting every provider mapped to it (`anthropic` and any user-mapped alias, such as a provider plugin whose id points at the same account).

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
