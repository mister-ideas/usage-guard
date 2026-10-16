"""Usage guard: block exactly at the subscription limit, on fresh data.

The only blocking point is a subscription usage window at/over the threshold
(default 100% used = 0% left). No account-level extra-usage or credit policy
is enforced.

Usage evidence lives in this plugin's own cache and is refreshed on a freshness
TTL, bounded by a per-source minimum interval and by any rate-limit backoff.
Every guarded ingress shares that cache, and concurrent ones collapse into a
single read, so the guard's own request rate cannot rate-limit the endpoint it
reads. A read that fails or is rate-limited keeps the last good snapshot and
decides on it; evidence that is stale, missing or malformed denies
(fail-closed), so a long run never acts on usage information the guard could
not confirm.

Every source is read through the documented
``agent.account_usage.fetch_account_usage``, so no credential reaches this plugin
and no usage endpoint is called from here. Note that upstream's Anthropic parser
scales any ``utilization`` <= 1 by 100 while that payload's values are
percentages, so a sub-1% window arrives inflated and the guard cannot repair it
from the public API. Refusal replies are marked
usage_guard_blocked so host auxiliary paths reject them before provider dispatch.
On stock Hermes this gates main-loop llm_execution dispatches, including MoA
turns (the running preset's enabled slots are resolved and checked). Auxiliary
calls — compression, vision, titles, and the MoA advisor fan-out — are
observer-only upstream and are NOT gated. Other clients, in-flight requests,
missing plugins, and credential mismatches remain outside this guard.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Optional

LOG = logging.getLogger("plugins.usage-guard")

DEFAULT_PROVIDER_KEYS: list[str] = [
    "anthropic",
    "openai-codex",
]

# provider-id -> account_usage data-source key. Stock fetchers exist for
# "anthropic", "openai-codex" and "openrouter"; custom provider plugins can
# expose their own fetch_account_usage hook and are gated by listing their
# provider id here (or in the provider_keys/provider_sources settings).
DEFAULT_PROVIDER_SOURCES: dict[str, str] = {
    "anthropic": "anthropic",
    "openai-codex": "openai-codex",
}

# data-source key -> value scale. The host contract for
# ``AccountUsageWindow.used_percent`` is a percentage (its producers clamp to
# 0-100). A source that reports 0-1 fractions must be declared here, or a full
# window (1.0) would read as 1% and never block.
DEFAULT_SOURCE_SCALES: dict[str, str] = {
    "anthropic": "percent",
    "openai-codex": "percent",
    "openrouter": "percent",
}

# data-source key -> smallest safe interval between two network reads, in
# seconds. The providers differ by orders of magnitude, so one global floor
# either starves the freshest source or hammers the most sensitive one:
#
#   anthropic      The OAuth usage endpoint has a small hourly budget per
#                  account that Claude Code itself and every other usage tool
#                  on the machine draw from. Pollers reading it every 30-60s
#                  are documented to hit a persistent 429 loop (and to starve
#                  Claude Code's own usage panel), so ~3 minutes is the
#                  smallest cadence that keeps working.
#   openai-codex   Same class of undocumented backend usage budget; the Codex
#                  TUI's own poller is known to hit 429 there.
#   openrouter     Publishes its limits (20 requests/minute on the free tier,
#                  scaling with credit balance) and tolerates far more.
#
# A source that is not declared here falls back to ``min_fetch_interval_s``.
DEFAULT_SOURCE_MIN_INTERVALS: dict[str, float] = {
    "anthropic": 180.0,
    "openai-codex": 180.0,
    "openrouter": 30.0,
}

DEFAULTS: dict[str, Any] = {
    "provider_keys": list(DEFAULT_PROVIDER_KEYS),
    "provider_sources": dict(DEFAULT_PROVIDER_SOURCES),
    "source_scales": dict(DEFAULT_SOURCE_SCALES),
    "source_min_intervals": dict(DEFAULT_SOURCE_MIN_INTERVALS),
    # Deny when ANY window's used_percent >= threshold (100 = fully used).
    # 100 = the only blocking point is the subscription limit itself (0% left);
    # extra usage may stay enabled on the accounts.
    "threshold_percent": 100.0,
    # Freshness target: evidence older than this triggers a refresh, subject to
    # min_fetch_interval_s (the hard rate cap) and any rate-limit backoff. There
    # is no per-request refresh mode: every guarded ingress shares one cache and
    # concurrent ones collapse into a single network read per interval.
    "max_cache_age_s": 60.0,
    # While a refresh is failing or rate-limited, the last good snapshot stays
    # usable for up to this long instead of refusing the request. 0 disables it
    # (a failed refresh then follows the stale-data policy immediately).
    "stale_grace_s": 900.0,
    # On the stale path, wait at most this long (seconds) for the refresh to
    # land before applying the stale-data policy. 0 disables the wait.
    "poll_wait_timeout_s": 20.0,
    # Hard floor between two network reads of one source, enforced across
    # processes through the cache. This is what keeps the usage endpoint from
    # rate-limiting us when several ingresses (main turn, auxiliary calls, MoA
    # slots, subagents) are guarded at once. 0 disables the floor.
    "min_fetch_interval_s": 30.0,
    # False: deny on stale/missing/unavailable data (recommended). True: allow.
    "fail_open": False,
}

_NOTICE_HEADER = (
    "⏸️ Usage guard blocked this request: the {source} usage data source "
    "reports an exhausted window for provider {provider}."
)
_EXHAUSTED_NOTICE = (
    "{windows} — the request was not sent to the provider. "
    "Switch /model to another provider or wait for the reset."
)
_UNAVAILABLE_NOTICE = (
    "Usage guard could not confirm remaining allowance for provider {provider} "
    "({reason}); fail-open is disabled, so the request was not sent to the "
    "provider. Fix the usage data source; do not disable safety checks to bypass this refusal."
)

_CACHE_DIRNAME = "usage-guard"
_CACHE_FILENAME = "usage.json"
_CACHE_LOCK_TIMEOUT_S = 10.0

# Rate-limit backoff on a rejected refresh: honour Retry-After, never wait less
# than an exponentially growing floor, cap the wait, and add jitter so several
# processes sharing one endpoint do not retry in lockstep.
_BACKOFF_BASE_S = 30.0
_BACKOFF_MAX_S = 900.0
_BACKOFF_JITTER = 0.2

# In-process lock; the file lock below covers cross-process (desktop backend
# and gateway share HERMES_HOME). RLock would hide nesting bugs; the merge
# helper below never nests _locked.
_cache_thread_lock = threading.Lock()

# Cross-platform advisory file lock: fcntl on POSIX, msvcrt on Windows.
try:
    import fcntl

    def _lock_fh(fh) -> None:
        # Non-blocking: _locked() retries on a deadline, so a stuck holder
        # can never hang a guarded request past _CACHE_LOCK_TIMEOUT_S.
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fh(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
except ImportError:  # pragma: no cover - windows path
    try:
        import msvcrt

        def _lock_fh(fh) -> None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

        def _unlock_fh(fh) -> None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:  # pragma: no cover - no advisory locking available
        def _lock_fh(fh) -> None:
            pass

        def _unlock_fh(fh) -> None:
            pass


@contextlib.contextmanager
def _locked(path: str):
    """Hold the cache's advisory lock (bounded wait, thread + process safe)."""
    fh = open(path + ".lock", "a+")
    try:
        deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT_S
        while True:
            try:
                _lock_fh(fh)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("usage-guard cache lock timed out") from None
                time.sleep(0.02)
        yield
    finally:
        try:
            _unlock_fh(fh)
        except Exception:
            pass
        fh.close()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp; naive values are read as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cache_path() -> str:
    """$HERMES_HOME/plugin-data/usage-guard/usage.json (HERMES_HOME honored)."""
    home = os.environ.get("HERMES_HOME", "").strip() or os.path.join(
        os.path.expanduser("~"), ".hermes"
    )
    return os.path.join(home, "plugin-data", _CACHE_DIRNAME, _CACHE_FILENAME)


def _read_cache(path: Optional[str] = None) -> dict[str, Any]:
    """Read the cache under the advisory lock; returns {} when absent/corrupt."""
    full = path or _cache_path()
    try:
        with _locked(full):
            with open(full, "r", encoding="utf-8") as fh:
                data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        LOG.debug("usage-guard cache read failed", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _merge_into_cache(source_key: str, record: dict[str, Any],
                      path: Optional[str] = None) -> None:
    """Merge one provider record into the on-disk cache atomically.

    The read-modify-write runs under the advisory file lock (plus the
    in-process lock), so concurrent pollers — same process or the desktop
    backend and gateway sharing HERMES_HOME — cannot drop each other's
    provider records.
    """
    full = path or _cache_path()
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with _cache_thread_lock, _locked(full):
        try:
            with open(full, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            if not isinstance(disk, dict):
                disk = {}
        except FileNotFoundError:
            disk = {}
        except Exception:
            LOG.debug("usage-guard cache read during merge failed", exc_info=True)
            disk = {}
        providers = disk.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        providers[source_key] = record
        disk["providers"] = providers
        disk["schema"] = 1
        tmp = f"{full}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(disk, fh, sort_keys=True)
        os.replace(tmp, full)


def _write_cache(cache: dict[str, Any], path: Optional[str] = None) -> None:
    """Replace the whole cache under the advisory lock (unique tmp + replace)."""
    full = path or _cache_path()
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with _cache_thread_lock, _locked(full):
            tmp = f"{full}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, sort_keys=True)
            os.replace(tmp, full)
    except Exception:
        LOG.debug("usage-guard cache write failed", exc_info=True)


# ── snapshot → cache record (our own serialization) ─────────────────────────

def _window_record(window: Any) -> Optional[dict[str, Any]]:
    label = getattr(window, "label", None)
    used = getattr(window, "used_percent", None)
    reset_at = getattr(window, "reset_at", None)
    if not isinstance(label, str) or not label:
        return None
    if used is None or isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    record: dict[str, Any] = {"label": label, "used_percent": float(used)}
    if isinstance(reset_at, datetime):
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        record["reset_at"] = reset_at.astimezone(timezone.utc).isoformat()
    return record


def _snapshot_record(snapshot: Any) -> dict[str, Any]:
    """Flatten an AccountUsageSnapshot-shaped object into a JSON record."""
    if snapshot is None:
        return {"ok": False, "reason": "no-data"}
    if not isinstance(getattr(snapshot, "fetched_at", None), datetime):
        return {"ok": False, "reason": "malformed-snapshot"}
    fetched_at = snapshot.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    reason = getattr(snapshot, "unavailable_reason", None)
    windows: list[dict[str, Any]] = []
    codex_rate_blocked = False
    if not reason:
        for window in getattr(snapshot, "windows", None) or ():
            record = _window_record(window)
            if record is not None:
                windows.append(record)
        raw = getattr(snapshot, "raw", None) or {}
        if not isinstance(raw, dict):
            return {"ok": False, "reason": "malformed-raw-usage"}
        # Core/provider adapters may expose only the classic four windows.
        # Preserve *all* account usage windows, including OAuth-app and
        # overage-included/model-entitlement buckets. These OAuth JSON values
        # are percentages, unlike fractional unified response-header values.
        for key, bucket in raw.items():
            if key != "five_hour" and key != "seven_day" and not key.startswith("seven_day_"):
                continue
            if bucket is None:
                continue
            if not isinstance(bucket, dict):
                return {"ok": False, "reason": "malformed-raw-window"}
            used = bucket.get("utilization")
            if used is None:
                continue
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                return {"ok": False, "reason": "malformed-raw-window"}
            window = {"label": key, "used_percent": float(used)}
            reset = bucket.get("resets_at")
            if reset is not None:
                if _parse_ts(reset) is None:
                    return {"ok": False, "reason": "malformed-raw-reset"}
                window["reset_at"] = reset
            windows.append(window)
        limits = raw.get("limits")
        if limits is not None:
            if not isinstance(limits, list):
                return {"ok": False, "reason": "malformed-limits"}
            for limit in limits:
                if not isinstance(limit, dict):
                    return {"ok": False, "reason": "malformed-limit"}
                used = limit.get("percent")
                if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                    return {"ok": False, "reason": "malformed-limit"}
                # Conservatively gate every published limit. Scope metadata is
                # not enough to prove the next model/context is exempt.
                window = {"label": "limit:" + str(limit.get("kind") or "unknown"), "used_percent": float(used)}
                reset = limit.get("resets_at")
                if reset is not None:
                    if _parse_ts(reset) is None:
                        return {"ok": False, "reason": "malformed-limit-reset"}
                    window["reset_at"] = reset
                windows.append(window)
        # Codex raw data carries main, per-model and review-specific limits.
        # Conservatively gate all scopes rather than assume a model is exempt.
        groups = []
        for key in ("rate_limit", "code_review_rate_limit"):
            group = raw.get(key)
            if group is not None:
                if not isinstance(group, dict):
                    return {"ok": False, "reason": "malformed-codex-limit"}
                groups.append((key, group))
        additional = raw.get("additional_rate_limits")
        if additional is not None:
            if not isinstance(additional, list):
                return {"ok": False, "reason": "malformed-codex-additional-limits"}
            for group in additional:
                if not isinstance(group, dict):
                    return {"ok": False, "reason": "malformed-codex-limit"}
                inner = group.get("rate_limit", group)
                if not isinstance(inner, dict):
                    return {"ok": False, "reason": "malformed-codex-limit"}
                groups.append((str(group.get("id") or "additional"), inner))
        for label, group in groups:
            for flag in ("allowed", "limit_reached"):
                if flag in group and not isinstance(group[flag], bool):
                    return {"ok": False, "reason": "malformed-codex-limit-flag"}
            codex_rate_blocked |= group.get("allowed") is False or group.get("limit_reached") is True
            for key in ("primary_window", "secondary_window"):
                bucket = group.get(key)
                if bucket is None:
                    continue
                if not isinstance(bucket, dict):
                    return {"ok": False, "reason": "malformed-codex-window"}
                used = bucket.get("used_percent")
                if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                    return {"ok": False, "reason": "malformed-codex-window"}
                window = {"label": "codex:" + label + ":" + key, "used_percent": float(used)}
                reset = bucket.get("reset_at")
                if reset is not None:
                    parsed = _parse_ts(reset) if isinstance(reset, str) else None
                    if parsed is not None:
                        window["reset_at"] = parsed.isoformat()
                    elif (isinstance(reset, (int, float)) and not isinstance(reset, bool)
                          and math.isfinite(reset)):
                        try:
                            window["reset_at"] = datetime.fromtimestamp(reset, timezone.utc).isoformat()
                        except (OverflowError, OSError, ValueError):
                            return {"ok": False, "reason": "malformed-codex-reset"}
                    else:
                        return {"ok": False, "reason": "malformed-codex-reset"}
                windows.append(window)
        if not windows:
            return {"ok": False, "reason": "no-usable-windows"}
    return {
        "ok": True,
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
        "plan": getattr(snapshot, "plan", None),
        "unavailable_reason": reason if isinstance(reason, str) else None,
        "windows": windows,
        "codex_rate_blocked": codex_rate_blocked,
    }


# ── policy evaluation ────────────────────────────────────────────────────────

def _fresh(record: Optional[dict[str, Any]], max_age_s: float, now: Optional[datetime] = None) -> bool:
    if not record:
        return False
    fetched = _parse_ts(record.get("fetched_at"))
    if fetched is None:
        return False
    now = now or _now_utc()
    try:
        age = (now - fetched).total_seconds()
    except OverflowError:
        return False
    return 0.0 <= age <= max_age_s


def _evidence_age_s(record: Optional[dict[str, Any]], now: datetime) -> Optional[float]:
    """Seconds since the snapshot's windows were captured, or None if unknown."""
    if not isinstance(record, dict):
        return None
    fetched = _parse_ts(record.get("fetched_at"))
    if fetched is None:
        return None
    try:
        age = (now - fetched).total_seconds()
    except OverflowError:
        return None
    return age if age >= 0.0 else None


_stale_evidence_lock = threading.Lock()
_stale_evidence_warned: set[str] = set()


def _warn_stale_evidence_once(source_key: str, age: float) -> None:
    """Log once per source that decisions are using the last good snapshot.

    A rate limit or a transport error is not evidence that allowance changed, so
    the guard keeps deciding on the last snapshot it actually read. The warning
    makes that visible without turning it into a refusal.
    """
    with _stale_evidence_lock:
        if source_key in _stale_evidence_warned:
            return
        _stale_evidence_warned.add(source_key)
    LOG.warning(
        "usage-guard: serving the last usage snapshot for %s (%.0fs old) because the usage "
        "endpoint is rate-limiting or failing; the exhaustion check still applies to it.",
        source_key, age,
    )


def _failure_record(source_key: str, previous: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    """Build the record for a failed refresh WITHOUT discarding good evidence.

    The windows and their ``fetched_at`` are carried forward: a rate limit says
    nothing about allowance, and ``_decide_provider`` decides how long the last
    good snapshot stays usable (``stale_grace_s``). Only a 429 sets a backoff,
    which suppresses new reads rather than refusing requests.
    """
    failures = previous.get("consecutive_failures")
    failures = failures + 1 if isinstance(failures, int) and not isinstance(failures, bool) else 1
    record: dict[str, Any] = {
        "ok": False,
        "unavailable_reason": "fetch-error",
        "last_error_at": _now_utc().isoformat(),
        "consecutive_failures": failures,
        "windows": list(previous.get("windows") or []),
        "fetched_at": previous.get("fetched_at"),
    }
    for carried in ("codex_rate_blocked",):
        if carried in previous:
            record[carried] = previous[carried]
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        retry_after = (getattr(getattr(exc, "response", None), "headers", {}) or {}).get("Retry-After")
        try:
            requested = float(retry_after) if retry_after else 0.0
        except (TypeError, ValueError):
            requested = 0.0
        grown = min(_BACKOFF_BASE_S * (2 ** (failures - 1)), _BACKOFF_MAX_S)
        backoff_s = min(max(requested, grown), _BACKOFF_MAX_S)
        # Jitter (derived from uuid4, so no extra import) spreads retries across
        # the desktop backend and the gateway sharing one endpoint.
        backoff_s *= 1.0 + _BACKOFF_JITTER * ((uuid.uuid4().int % 1000) / 1000.0)
        record["backoff_until"] = (_now_utc() + timedelta(seconds=backoff_s)).isoformat()
        LOG.warning(
            "usage-guard: usage endpoint rate-limited the refresh for %s; backing off %.0fs",
            source_key, backoff_s,
        )
    return record


def _exhausted_windows(
    record: Optional[dict[str, Any]], threshold_percent: float, now: Optional[datetime] = None
) -> list[dict[str, Any]]:
    """Windows at/over threshold (any reset state). Callers decide policy:
    `_decide_provider` denies on past resets before consulting this list."""
    now = now or _now_utc()
    hits: list[dict[str, Any]] = []
    for window in (record or {}).get("windows") or []:
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        if float(used) < threshold_percent:
            continue
        hits.append(window)
    return hits


def _fmt_reset(window: dict[str, Any]) -> str:
    reset_at = _parse_ts(window.get("reset_at"))
    if reset_at is None:
        return "unknown"
    local = reset_at.astimezone()
    return local.strftime("%Y-%m-%d %H:%M %Z")


# ── background poller (at most one in flight per provider, bounded) ─────────

_poll_lock = threading.Lock()
_poll_inflight: set[str] = set()


def _spawn_poll(source_key: str, fetcher: Callable[[], Any], sync_timeout_s: float = 0.0) -> bool:
    """Start one bounded background fetch; returns False if one is in flight.

    With ``sync_timeout_s`` > 0 the caller waits up to that many seconds for
    the fetch to finish, so the decision can use the refreshed snapshot
    instead of denying on stale data.
    """
    with _poll_lock:
        already_inflight = source_key in _poll_inflight
        if not already_inflight:
            _poll_inflight.add(source_key)
    if already_inflight:
        # Never hold the lock while waiting: the worker needs it to finish.
        deadline = time.monotonic() + sync_timeout_s
        while time.monotonic() < deadline:
            with _poll_lock:
                if source_key not in _poll_inflight:
                    break
            time.sleep(0.05)
        return False

    def _run() -> None:
        previous = _read_cache().get("providers", {}).get(source_key)
        previous = previous if isinstance(previous, dict) else {}
        try:
            snapshot = fetcher()
            record = _snapshot_record(snapshot)
            if record.get("ok") and not record.get("unavailable_reason") and not record.get("windows"):
                record = {"ok": False, "reason": "no-usable-windows"}
        except Exception as exc:
            LOG.debug("usage-guard poll for %s failed", source_key, exc_info=True)
            # A failed refresh keeps the last good evidence and never refuses a
            # request on its own; only a 429 sets a backoff, and a backoff
            # suppresses new reads rather than denying dispatch.
            record = _failure_record(source_key, previous, exc)
        record["last_attempt_at"] = _now_utc().isoformat()
        record["polled_at"] = _now_utc().isoformat()
        try:
            _merge_into_cache(source_key, record)
        except Exception:
            LOG.debug("usage-guard cache update for %s failed", source_key, exc_info=True)
        finally:
            with _poll_lock:
                _poll_inflight.discard(source_key)

    try:
        thread = threading.Thread(
            target=_run, name=f"usage-guard-poll-{source_key}", daemon=True
        )
        thread.start()
        if sync_timeout_s > 0.0:
            # Bounded wait so the caller can decide on the refreshed snapshot
            # instead of denying on stale data.
            thread.join(timeout=sync_timeout_s)
        return True
    except Exception:
        with _poll_lock:
            _poll_inflight.discard(source_key)
        return False


# ── config ───────────────────────────────────────────────────────────────────

def _clean_string_list(value: Any) -> Optional[list[str]]:
    if not isinstance(value, list):
        return None
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            return None
        cleaned.append(item)
    return cleaned


def _clean_sources(value: Any) -> Optional[dict[str, str]]:
    if not isinstance(value, dict):
        return None
    sources: dict[str, str] = {}
    for key, mapped in value.items():
        if (
            not isinstance(key, str) or not key
            or not isinstance(mapped, str) or not mapped
        ):
            return None
        sources[key] = mapped
    return sources


def _clean_scales(value: Any) -> Optional[dict[str, str]]:
    """Validate ``source_scales``: data-source key -> 'percent' | 'fraction'."""
    if not isinstance(value, dict):
        return None
    scales: dict[str, str] = {}
    for key, scale in value.items():
        if not isinstance(key, str) or not key:
            return None
        if scale not in ("percent", "fraction"):
            return None
        scales[key] = scale
    return scales


def _clean_intervals(value: Any) -> Optional[dict[str, float]]:
    """Validate ``source_min_intervals``: data-source key -> seconds (>= 0)."""
    if not isinstance(value, dict):
        return None
    intervals: dict[str, float] = {}
    for key, seconds in value.items():
        if not isinstance(key, str) or not key:
            return None
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        intervals[key] = float(seconds)
    return intervals


def _source_interval(source_key: str, settings: dict[str, Any]) -> float:
    """The hard floor between two reads of this source, in seconds.

    Providers differ by orders of magnitude, so the declared per-source value
    wins and an undeclared source falls back to the global floor.
    """
    declared = settings.get("source_min_intervals") or {}
    if source_key in declared:
        return float(declared[source_key])
    return float(settings["min_fetch_interval_s"])


def _threshold(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 100 else None


def _max_age(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    age = float(value)
    return age if math.isfinite(age) and age >= 0 else None


def _read_settings(get_config: Callable[[str, Any], Any]) -> tuple[dict[str, Any], bool]:
    """Read and validate settings. Returns (settings, config_ok).

    A malformed setting falls back to its default (and flags config_ok=False,
    which the middleware treats as a deny) rather than silently weakening the
    guard.
    """
    settings = dict(DEFAULTS)
    config_ok = True
    try:
        raw_keys = get_config("provider_keys", DEFAULT_PROVIDER_KEYS)
        keys = _clean_string_list(raw_keys)
        if keys is None:
            config_ok = False
            LOG.warning("usage-guard: malformed provider_keys %r; using default", raw_keys)
        else:
            settings["provider_keys"] = keys

        raw_sources = get_config("provider_sources", DEFAULT_PROVIDER_SOURCES)
        sources = _clean_sources(raw_sources)
        if sources is None:
            config_ok = False
            LOG.warning("usage-guard: malformed provider_sources %r; using default", raw_sources)
        else:
            settings["provider_sources"] = sources

        raw_scales = get_config("source_scales", DEFAULT_SOURCE_SCALES)
        scales = _clean_scales(raw_scales)
        if scales is None:
            config_ok = False
            LOG.warning("usage-guard: malformed source_scales %r; using default", raw_scales)
        else:
            settings["source_scales"] = scales

        threshold = _threshold(get_config("threshold_percent", DEFAULTS["threshold_percent"]))
        if threshold is None:
            config_ok = False
            LOG.warning("usage-guard: malformed threshold_percent; using default")
        else:
            settings["threshold_percent"] = threshold

        max_age = _max_age(get_config("max_cache_age_s", DEFAULTS["max_cache_age_s"]))
        if max_age is None:
            config_ok = False
            LOG.warning("usage-guard: malformed max_cache_age_s; using default")
        else:
            settings["max_cache_age_s"] = max_age

        poll_wait = _max_age(get_config("poll_wait_timeout_s", DEFAULTS["poll_wait_timeout_s"]))
        if poll_wait is None:
            config_ok = False
            LOG.warning("usage-guard: malformed poll_wait_timeout_s; using default")
        else:
            settings["poll_wait_timeout_s"] = poll_wait

        stale_grace = _max_age(get_config("stale_grace_s", DEFAULTS["stale_grace_s"]))
        if stale_grace is None:
            config_ok = False
            LOG.warning("usage-guard: malformed stale_grace_s; using default")
        else:
            settings["stale_grace_s"] = stale_grace

        min_interval = _max_age(get_config("min_fetch_interval_s", DEFAULTS["min_fetch_interval_s"]))
        if min_interval is None:
            config_ok = False
            LOG.warning("usage-guard: malformed min_fetch_interval_s; using default")
        else:
            settings["min_fetch_interval_s"] = min_interval

        raw_intervals = get_config("source_min_intervals", DEFAULT_SOURCE_MIN_INTERVALS)
        intervals = _clean_intervals(raw_intervals)
        if intervals is None:
            config_ok = False
            LOG.warning("usage-guard: malformed source_min_intervals %r; using default", raw_intervals)
        else:
            settings["source_min_intervals"] = intervals

        fail_open = get_config("fail_open", DEFAULTS["fail_open"])
        if not isinstance(fail_open, bool):
            config_ok = False
            LOG.warning("usage-guard: malformed fail_open %r; using default", fail_open)
        else:
            settings["fail_open"] = fail_open


    except Exception:
        config_ok = False
        LOG.warning("usage-guard: configuration unreadable; defaults + deny apply")
    return settings, config_ok


# ── decision ────────────────────────────────────────────────────────────────

class _Decision:
    __slots__ = ("allow", "notice", "reason")

    def __init__(self, allow: bool, notice: str = "", reason: str = "") -> None:
        self.allow = allow
        self.notice = notice
        self.reason = reason


_undeclared_source_lock = threading.Lock()
_undeclared_sources_warned: set[str] = set()


def _warn_undeclared_source_once(source_key: str, record: Any) -> None:
    """Flag an unclassified source whose values look like fractions (0-1).

    ``used_percent`` is a percentage per the host contract, so 1.0 means 1% used;
    a source that actually reports fractions would read a full window as 1% and
    never block. Only sources absent from ``source_scales`` are flagged, and only
    when every window value is <= 1 — a genuine idle account is indistinguishable,
    so this is a warning, not a denial.
    """
    windows = record.get("windows") if isinstance(record, dict) else None
    if not isinstance(windows, list) or not windows:
        return
    values: list[float] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used):
            values.append(float(used))
    if not values or any(value > 1.0 for value in values):
        return
    with _undeclared_source_lock:
        if source_key in _undeclared_sources_warned:
            return
        _undeclared_sources_warned.add(source_key)
    LOG.warning(
        "usage-guard: source %s is not declared in source_scales and reports only values <= 1; "
        "they are read as percentages (1.0 = 1%% used). If this source reports fractions, "
        "declare it in source_scales as 'fraction'.",
        source_key,
    )


def _scaled_record(record: Optional[dict[str, Any]], scale: str) -> Optional[dict[str, Any]]:
    """Copy a record with every window normalized to the guard's percentage scale.

    ``percent`` (the host contract) passes through untouched; ``fraction`` scales
    0-1 values by 100. Non-numeric values are left for the window validator to
    reject, so a malformed source can never be silently compared.
    """
    if not isinstance(record, dict) or scale != "fraction":
        return record
    windows = record.get("windows")
    if not isinstance(windows, list):
        return record
    scaled: list[Any] = []
    for window in windows:
        if isinstance(window, dict):
            used = window.get("used_percent")
            if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used):
                window = {**window, "used_percent": float(used) * 100.0}
        scaled.append(window)
    return {**record, "windows": scaled}


def _decide_provider(
    provider: str, source_key: str, settings: dict[str, Any], config_ok: bool,
    now: Optional[datetime] = None, spawn_poll: bool = True,
) -> _Decision:
    now = now or _now_utc()
    record = _read_cache().get("providers", {}).get(source_key)
    if not isinstance(record, dict):
        record = None

    # A rate limit or a transport error is not evidence about allowance, so it
    # never refuses a request on its own. While a refresh is failing, the last
    # good snapshot stays usable for up to ``stale_grace_s`` and the exhaustion
    # interlock applies to it unchanged; the backoff only suppresses new reads.
    backoff_until = _parse_ts((record or {}).get("backoff_until")) if isinstance(record, dict) else None
    backing_off = backoff_until is not None and backoff_until > now
    evidence_age = _evidence_age_s(record, now)
    last_read_failed = bool((record or {}).get("unavailable_reason"))
    stale_usable = (
        evidence_age is not None
        and evidence_age <= settings["stale_grace_s"]
        and (backing_off or last_read_failed)
    )

    if not backing_off and not _fresh(record, settings["max_cache_age_s"], now):
        if spawn_poll:
            # Refresh bounded-synchronously so the decision uses fresh data
            # instead of denying the first request after the cache ages out.
            # Concurrent callers — same process or another — share this one
            # read: the claim below is atomic and the others wait for it.
            _spawn_poll_for_source(
                source_key,
                sync_timeout_s=settings["poll_wait_timeout_s"],
                min_interval_s=_source_interval(source_key, settings),
            )
            refreshed = _read_cache().get("providers", {}).get(source_key)
            if isinstance(refreshed, dict):
                record = refreshed
        # Re-evaluate on the record the refresh produced: a read that failed
        # marks the record and keeps its windows, and those windows are what
        # decides — so a refresh that could not land never refuses on its own.
        backoff_until = _parse_ts(record.get("backoff_until")) if isinstance(record, dict) else None
        backing_off = backoff_until is not None and backoff_until > now
        evidence_age = _evidence_age_s(record, now)
        last_read_failed = bool((record or {}).get("unavailable_reason"))
        stale_usable = (
            evidence_age is not None
            and evidence_age <= settings["stale_grace_s"]
            and (backing_off or last_read_failed)
        )

    if not _fresh(record, settings["max_cache_age_s"], _now_utc()) and not stale_usable:
        reason = (
            record.get("unavailable_reason")
            if isinstance(record, dict) and record.get("unavailable_reason")
            else "no fresh snapshot"
        )
        if settings["fail_open"] and config_ok:
            return _Decision(True, reason=reason)
        return _Decision(
            False,
            _UNAVAILABLE_NOTICE.format(provider=provider, reason=reason),
            reason=reason,
        )

    if stale_usable and not _fresh(record, settings["max_cache_age_s"], _now_utc()):
        _warn_stale_evidence_once(source_key, evidence_age or 0.0)

    # The latest read failed and left no windows to evaluate: there is nothing
    # to decide on, so the unavailable policy applies (fail_open honored).
    if isinstance(record, dict) and record.get("unavailable_reason") and not record.get("windows"):
        reason = str(record.get("unavailable_reason"))
        if settings["fail_open"] and config_ok:
            return _Decision(True, reason=reason)
        return _Decision(
            False,
            _UNAVAILABLE_NOTICE.format(provider=provider, reason=reason),
            reason=reason,
        )

    # Normalize to the guard's percentage scale before validating: the host
    # contract is a percentage, a declared fractional source is scaled ×100, and
    # an out-of-range result denies below instead of being compared.
    source_scale = settings["source_scales"].get(source_key, "percent")
    if source_key not in settings["source_scales"]:
        _warn_undeclared_source_once(source_key, record)
    record = _scaled_record(record, source_scale)

    windows = record.get("windows")
    if not isinstance(windows, list) or not windows:
        # Reached only with no usable evidence at all: the staleness policy
        # above accepts a within-grace snapshot whose latest refresh failed
        # (``ok`` false, windows carried forward), so a populated window list is
        # the actual requirement.
        return _Decision(False, "Usage guard: usable allowance evidence is missing; the request was not sent.", "invalid allowance evidence")
    for window in windows:
        if not isinstance(window, dict):
            return _Decision(False, "Usage guard: malformed allowance window; the request was not sent.", "invalid window")
        used = window.get("used_percent")
        reset = window.get("reset_at")
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not math.isfinite(used) or used < 0 or used > 100
                or (reset is not None and _parse_ts(reset) is None)):
            return _Decision(False, "Usage guard: malformed allowance window; the request was not sent.", "invalid window")

    if source_key == "openai-codex" and record.get("codex_rate_blocked") is True:
        return _Decision(False, "Usage guard: Codex reports a blocked or reached limit; the request was not sent.", "Codex server limit")

    # A past reset is not proof of replenishment. Refuse until a new snapshot
    # reports a future reset rather than guessing an exhausted window cleared.
    for window in record.get("windows") or []:
        reset = _parse_ts(window.get("reset_at")) if isinstance(window, dict) else None
        if reset is not None and reset <= now:
            return _Decision(False, "Usage guard: usage reset has not been confirmed by fresh data; the request was not sent.", "unconfirmed reset")

    hits = _exhausted_windows(record, settings["threshold_percent"], now)
    if not hits:
        return _Decision(True)
    windows_txt = "; ".join(
        f"{w.get('label')}: {float(w.get('used_percent') or 0):.0f}% used, "
        f"resets {_fmt_reset(w)}"
        for w in hits
    )
    plan = record.get("plan")
    header = _NOTICE_HEADER.format(
        source=source_key, provider=f"{provider}" + (f" ({plan})" if plan else "")
    )
    return _Decision(
        False,
        f"{header} {_EXHAUSTED_NOTICE.format(windows=windows_txt)}",
        reason=windows_txt,
    )


def _claim_fetch_slot(source_key: str, min_interval_s: float) -> bool:
    """Atomically claim the right to read this source's usage.

    The claim is read and written under the cache's advisory file lock, so N
    concurrent guarded ingresses — several threads in one process, or the desktop
    backend and the gateway sharing HERMES_HOME — collapse into one request to
    the usage endpoint per interval instead of N. The claim is stamped before the
    fetch starts, so a second caller can tell a read is in flight (see
    ``_await_inflight``). Returns True when the caller may fetch.
    """
    if min_interval_s <= 0.0:
        return True
    full = _cache_path()
    now = _now_utc()
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with _cache_thread_lock, _locked(full):
        try:
            with open(full, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            if not isinstance(disk, dict):
                disk = {}
        except FileNotFoundError:
            disk = {}
        except Exception:
            LOG.debug("usage-guard cache read during claim failed", exc_info=True)
            disk = {}
        providers = disk.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        record = providers.get(source_key)
        last_attempt = _parse_ts(record.get("last_attempt_at")) if isinstance(record, dict) else None
        if last_attempt is not None:
            try:
                if (now - last_attempt).total_seconds() < min_interval_s:
                    return False
            except OverflowError:
                pass
        providers[source_key] = {**(record if isinstance(record, dict) else {}),
                                 "last_attempt_at": now.isoformat()}
        disk["providers"] = providers
        disk["schema"] = 1
        tmp = f"{full}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(disk, fh, sort_keys=True)
        os.replace(tmp, full)
    return True


def _await_inflight(source_key: str, timeout_s: float) -> None:
    """Wait (bounded) for an in-flight read of this source to record its result.

    ``last_attempt_at`` is stamped when a read starts and ``polled_at`` when one
    finishes, so ``last_attempt_at > polled_at`` means a read is running — in
    another thread or another process. Waiting for it lets a concurrent caller
    decide on that result instead of refusing on the stale record. When the floor
    (not an in-flight read) is what refused the claim, this returns at once.
    """
    if timeout_s <= 0.0:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = _read_cache().get("providers", {}).get(source_key)
        if not isinstance(record, dict):
            return
        attempt = _parse_ts(record.get("last_attempt_at"))
        polled = _parse_ts(record.get("polled_at"))
        if attempt is None or polled is None or attempt <= polled:
            return
        time.sleep(0.1)


def _spawn_poll_for_source(
    source_key: str, sync_timeout_s: float = 0.0, min_interval_s: float = 0.0
) -> None:
    """Best-effort: start one bounded fetch of the source's usage.

    ``min_interval_s`` > 0 is a hard floor between network reads of this source.
    The slot is claimed atomically under the cache's file lock, so N concurrent
    guarded ingresses — in this process or across the desktop backend and the
    gateway sharing HERMES_HOME — collapse into a single request to the usage
    endpoint. Callers that lose the claim wait (bounded) for the winner's result
    rather than deciding on a stale record.

    The core import happens inside the worker thread — if the core module is
    absent the poll records ``fetch-error`` and the guard keeps its on-disk
    policy instead of crashing the request.

    The Anthropic source is read directly (see ``_anthropic_snapshot``): the
    host fetcher inflates any sub-1% window ×100. Every other source uses the
    host fetcher.

    With ``sync_timeout_s`` > 0 the caller waits (bounded) for the fetch so a
    fail-closed decision can be made against fresh data rather than stale
    data. The wait is capped well below a turn budget; on timeout the stale
    snapshot policy still applies.
    """

    def _fetch() -> Any:
        # Every source, including anthropic, goes through Hermes's own usage
        # route: the core fetcher resolves the provider's stored sign-in and
        # attaches it itself, so the plugin never holds a credential.
        from agent.account_usage import fetch_account_usage as core_fetch

        return core_fetch(source_key)

    if not _claim_fetch_slot(source_key, min_interval_s):
        # A read of this source is already in flight (this process or another
        # sharing HERMES_HOME), or the interval floor is still in force. Wait
        # only while one is genuinely running, so the decision can use its
        # result instead of refusing on the stale record.
        _await_inflight(source_key, sync_timeout_s)
        return

    _spawn_poll(source_key, _fetch, sync_timeout_s=sync_timeout_s)


# ── MoA (virtual provider) slots ────────────────────────────────────────────

# The slots a MoA turn fans out to when the preset names none. These mirror the
# defaults Hermes applies to an unconfigured preset; the host-contract test
# asserts they still match the core's own constants.
_MOA_DEFAULT_REFERENCE_MODELS: tuple[dict[str, str], ...] = (
    {"provider": "openai-codex", "model": "gpt-5.5"},
    {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
)
_MOA_DEFAULT_AGGREGATOR: dict[str, str] = {
    "provider": "openrouter", "model": "anthropic/claude-opus-4.8"}


def _moa_clean_slot(slot: Any, *, include_enabled: bool = False) -> Optional[dict[str, Any]]:
    """A provider slot in the host's shape, or None when it is not one."""
    if not isinstance(slot, dict):
        return None
    provider = str(slot.get("provider") or "").strip()
    if not provider:
        return None
    clean: dict[str, Any] = {"provider": provider, "model": str(slot.get("model") or "").strip()}
    if include_enabled:
        clean["enabled"] = slot.get("enabled", True) is not False
    return clean


def _moa_normalize_preset(raw: Any) -> dict[str, Any]:
    """One preset in the shape ``_moa_slot_providers`` reads."""
    if not isinstance(raw, dict):
        raw = {}
    slots = [slot for slot in (_moa_clean_slot(item, include_enabled=True)
                               for item in (raw.get("reference_models") or [])) if slot]
    aggregator = _moa_clean_slot(raw.get("aggregator"))
    return {
        "enabled": raw.get("enabled", True) is not False,
        "reference_models": slots or [dict(slot, enabled=True) for slot in _MOA_DEFAULT_REFERENCE_MODELS],
        "aggregator": aggregator or dict(_MOA_DEFAULT_AGGREGATOR),
    }


def _normalize_moa_config(moa_raw: Any) -> dict[str, Any]:
    """Validate a raw ``moa`` config into ``{presets, default_preset}``.

    Implemented from the host's documented semantics rather than importing the
    core's own normalizer, so the guard reaches Hermes only through surfaces a
    plugin is allowed to use.
    """
    if not isinstance(moa_raw, dict):
        moa_raw = {}
    presets_raw = moa_raw.get("presets")
    presets: dict[str, dict[str, Any]] = {}
    if isinstance(presets_raw, dict):
        for name, preset in presets_raw.items():
            clean_name = str(name or "").strip()
            if clean_name:
                presets[clean_name] = _moa_normalize_preset(preset)
    if not presets:
        presets["default"] = _moa_normalize_preset(moa_raw)
    default_name = str(moa_raw.get("default_preset") or "").strip()
    if not default_name or default_name not in presets:
        default_name = next(iter(presets))
    return {"default_preset": default_name, "presets": presets}


def _moa_slot_providers(moa_raw: Any, model: Any) -> Optional[tuple[str, list[str]]]:
    """``(preset_name, slot_providers)`` for the MoA preset a turn runs, or None.

    ``slot_providers`` are the enabled reference-slot providers followed by the
    aggregator provider, in preset order. None means the preset could not be
    resolved (no core resolver, malformed config, unknown preset).
    """
    try:
        cfg = _normalize_moa_config(moa_raw)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    presets = cfg.get("presets")
    if not isinstance(presets, dict) or not presets:
        return None
    name = str(model or "").strip()
    if name not in presets:
        name = str(cfg.get("default_preset") or "").strip()
    preset = presets.get(name)
    if not isinstance(preset, dict):
        return None
    providers: list[str] = []
    if preset.get("enabled", True):
        for slot in preset.get("reference_models") or []:
            if not isinstance(slot, dict) or not slot.get("enabled", True):
                continue
            provider = str(slot.get("provider") or "").strip()
            if provider:
                providers.append(provider)
    aggregator = preset.get("aggregator")
    if isinstance(aggregator, dict):
        provider = str(aggregator.get("provider") or "").strip()
        if provider:
            providers.append(provider)
    return name, providers


def _moa_preset_slots(model: Any) -> Optional[tuple[str, list[str]]]:
    """Resolve the running MoA preset's slot providers from the live config."""
    try:
        from hermes_cli.config import load_config

        moa_raw = (load_config() or {}).get("moa")
    except Exception:
        return None
    return _moa_slot_providers(moa_raw, model)


def _gate_moa(kwargs: dict[str, Any], settings: dict[str, Any], config_ok: bool,
              downstream: Callable[[Any], Any], request: Any) -> Any:
    """Gate one MoA turn: deny when any guarded preset slot's provider is exhausted.

    Stock Hermes cannot gate the advisor fan-out itself (it runs in request
    assembly, before this middleware, as an observer-only auxiliary call); the
    denial stops the aggregator dispatch and every later iteration.
    """
    resolved = _moa_preset_slots(kwargs.get("model"))
    if resolved is None:
        if settings["fail_open"] and config_ok:
            LOG.warning("usage-guard: could not resolve the MoA preset %r; allowing (fail_open)",
                        kwargs.get("model"))
            return downstream(request)
        LOG.warning("usage-guard: could not resolve the MoA preset %r; denying", kwargs.get("model"))
        return _blocked_response(
            kwargs.get("api_mode"), kwargs.get("model"),
            "Usage guard: this MoA preset could not be resolved, so its providers could not be "
            "checked against the usage policy; fail-open is disabled, so the request was not sent.",
        )
    preset_name, slot_providers = resolved
    for slot_provider in slot_providers:
        if slot_provider not in settings["provider_keys"]:
            continue
        source_key = settings["provider_sources"].get(slot_provider, slot_provider)
        decision = _decide_provider(slot_provider, source_key, settings, config_ok)
        if not decision.allow:
            LOG.warning("usage-guard denied MoA preset=%s slot_provider=%s model=%s (%s)",
                        preset_name, slot_provider, kwargs.get("model"), decision.reason)
            return _blocked_response(kwargs.get("api_mode"), kwargs.get("model"), decision.notice)
    return downstream(request)


# ── response shaping (per documented Hermes adapter shapes) ──────────────────

def _blocked_response(api_mode: Any, model: Any, text: str) -> Any:
    model = model if isinstance(model, str) else ""
    if api_mode == "codex_responses":
        return SimpleNamespace(
            usage_guard_blocked=True, id="usage-guard-block", object="response",
            created_at=int(time.time()), model=model, status="completed",
            output=[{"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": text}]}],
            output_text=text,
        )
    if api_mode == "anthropic_messages":
        return SimpleNamespace(
            usage_guard_blocked=True, id="msg_usage_guard", model=model, role="assistant",
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )
    if api_mode != "chat_completions":
        LOG.error("usage-guard blocked unrecognized api_mode; response may be rejected")
    return SimpleNamespace(
        usage_guard_blocked=True, id="usage-guard-block", object="chat.completion",
        created=int(time.time()), model=model,
        choices=[SimpleNamespace(
            index=0, finish_reason="stop",
            message=SimpleNamespace(role="assistant", content=text, tool_calls=None),
        )],
        usage=SimpleNamespace(
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            prompt_tokens_details=None, completion_tokens_details=None,
        ),
    )


# ── middleware ───────────────────────────────────────────────────────────────

def gate_llm_execution(config_reader: Callable[[str, Any], Any], **kwargs: Any) -> Any:
    """Middleware body. All exceptions are contained: an unexpected error in
    this function would otherwise fail OPEN at the Hermes dispatcher (its
    middleware exception path runs the downstream call), so every branch here
    either calls next_call exactly once or returns a synthetic response."""
    next_call = kwargs.get("next_call")
    dispatched = False

    def downstream(request):
        nonlocal dispatched
        dispatched = True
        return next_call(request)
    request = kwargs.get("request")
    if not callable(next_call):
        return _blocked_response(kwargs.get("api_mode"), kwargs.get("model"),
                                 "Usage guard: malformed middleware payload.")
    provider = kwargs.get("provider")
    provider = provider if isinstance(provider, str) and provider else ""
    if not provider:
        return downstream(request)
    try:
        settings, config_ok = _read_settings(config_reader)
        # A malformed/unreadable configuration denies (fail-closed): the user
        # installed this plugin to prevent provider calls, so a broken policy
        # must never quietly disable the guard.
        if not config_ok:
            LOG.warning("usage-guard: malformed configuration; denying request")
            return _blocked_response(
                kwargs.get("api_mode"), kwargs.get("model"),
                "Usage guard: its configuration is malformed, so the request was "
                "not sent to the provider. Fix plugins.entries.usage-guard.settings "
                "or remove/uninstall the plugin.",
            )
        if provider == "moa":
            return _gate_moa(kwargs, settings, config_ok, downstream, request)
        if provider not in settings["provider_keys"]:
            return downstream(request)
        source_key = settings["provider_sources"].get(provider, provider)
        decision = _decide_provider(provider, source_key, settings, config_ok)
        if decision.allow:
            return downstream(request)
        LOG.warning("usage-guard denied provider=%s model=%s (%s)",
                    provider, kwargs.get("model"), decision.reason)
        return _blocked_response(kwargs.get("api_mode"), kwargs.get("model"), decision.notice)
    except Exception:
        if dispatched:
            # Preserve provider errors/interruption and normal retry semantics.
            # A call already dispatched must never be described as unsent.
            raise
        # Never raise into the fail-open dispatcher: deny with a notice.
        LOG.exception("usage-guard: unexpected error; denying request")
        try:
            return _blocked_response(
                kwargs.get("api_mode"), kwargs.get("model"),
                "Usage guard: internal error; the request was not sent to the provider.",
            )
        except Exception:  # pragma: no cover - last-resort belt
            LOG.exception("usage-guard: could not shape denial response")
            raise


def register(ctx) -> None:
    def middleware(**kwargs: Any) -> Any:
        return gate_llm_execution(ctx.get_config, **kwargs)

    ctx.register_middleware("llm_execution", middleware)
