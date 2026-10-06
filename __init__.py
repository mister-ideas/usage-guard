"""Usage guard: block exactly at the subscription limit, on fresh data.

The only blocking point is a subscription usage window at/over the threshold
(default 100% used = 0% left). No account-level extra-usage or credit policy
is enforced. Snapshots are refreshed as often as needed: at every guarded
ingress by default (with an unconfirmed-refresh deny), bounded synchronous
wait on the stale path, fail-closed on stale/missing/malformed evidence — so
long runs never act on late usage information.

Own account-usage cache; no quota-plugin dependency. Refusal replies are marked
usage_guard_blocked so host auxiliary paths reject them before provider dispatch.
Requires companion host auxiliary execution-middleware integration. Other clients,
in-flight requests, missing plugins, and credential mismatches remain outside
this guard.
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

DEFAULTS: dict[str, Any] = {
    "provider_keys": list(DEFAULT_PROVIDER_KEYS),
    "provider_sources": dict(DEFAULT_PROVIDER_SOURCES),
    # Deny when ANY window's used_percent >= threshold (100 = fully used).
    # 100 = the only blocking point is the subscription limit itself (0% left);
    # extra usage may stay enabled on the accounts.
    "threshold_percent": 100.0,
    # Snapshots older than this are stale (see module policy).
    "max_cache_age_s": 60.0,
    # On the stale path, wait at most this long (seconds) for the refresh to
    # land before applying the stale-data policy. 0 disables the wait.
    "poll_wait_timeout_s": 20.0,
    # False: deny on stale/missing/unavailable data (recommended). True: allow.
    "fail_open": False,
    "refresh_every_request": True,
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


# ── snapshot → cache record (our own serialization, no quota plugin code) ────

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
        "raw_present": bool(getattr(snapshot, "raw", None)),
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
        try:
            snapshot = fetcher()
            record = _snapshot_record(snapshot)
            if record.get("ok") and not record.get("unavailable_reason") and not record.get("windows"):
                record = {"ok": False, "reason": "no-usable-windows"}
        except Exception as exc:
            LOG.debug("usage-guard poll for %s failed", source_key, exc_info=True)
            record = {
                "ok": False, "unavailable_reason": "fetch-error", "windows": [],
                "fetched_at": _now_utc().isoformat(),
            }
            # M1: the usage endpoint rate-limiting us must not turn into a
            # tight retry loop. Record a bounded backoff; decisions deny
            # (fail-closed) until it expires — never reuse stale evidence.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429:
                retry_after = (getattr(getattr(exc, "response", None), "headers", {}) or {}).get("Retry-After")
                try:
                    backoff_s = min(max(float(retry_after), 1.0), 600.0) if retry_after else 60.0
                except (TypeError, ValueError):
                    backoff_s = 60.0
                record["backoff_until"] = (_now_utc() + timedelta(seconds=backoff_s)).isoformat()
                LOG.warning("usage-guard: usage endpoint rate-limited the refresh for %s; backing off %.0fs",
                            source_key, backoff_s)
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

        for key in ("refresh_every_request",):
            value = get_config(key, DEFAULTS[key])
            if not isinstance(value, bool):
                config_ok = False
            else:
                settings[key] = value

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


_raw_warning_lock = threading.Lock()
_raw_warning_shown = False


def raw_warning_done() -> bool:
    """True after the once-per-process raw-missing warning has been logged."""
    global _raw_warning_shown
    with _raw_warning_lock:
        return _raw_warning_shown


def _mark_raw_warning() -> None:
    global _raw_warning_shown
    with _raw_warning_lock:
        _raw_warning_shown = True


def _decide_provider(
    provider: str, source_key: str, settings: dict[str, Any], config_ok: bool,
    now: Optional[datetime] = None, spawn_poll: bool = True,
) -> _Decision:
    now = now or _now_utc()
    record = _read_cache().get("providers", {}).get(source_key)
    if not isinstance(record, dict):
        record = None

    # M1: the usage endpoint rate-limited the last refresh. Deny until the
    # backoff expires — regardless of cache freshness — never reuse evidence
    # gathered before a rejected refresh to allow a dispatch.
    backoff_until = _parse_ts((record or {}).get("backoff_until")) if isinstance(record, dict) else None
    if backoff_until is not None and backoff_until > now:
        return _Decision(
            False,
            "Usage guard: the usage endpoint is temporarily rate-limiting quota checks "
            f"(backing off until {backoff_until.astimezone().strftime('%H:%M:%S')}); "
            "the request was not sent. The next request after the backoff re-checks usage.",
            "usage-endpoint backoff",
        )

    if settings.get("refresh_every_request", True) or not _fresh(record, settings["max_cache_age_s"], now):
        previous_fetched = (record or {}).get("fetched_at")
        previous_polled = (record or {}).get("polled_at")
        if spawn_poll:
            # Refresh bounded-synchronously so the decision uses fresh data
            # instead of denying the first request after the cache ages out.
            # A poll for this source may already be in flight from another
            # request — the call below waits for that one to land.
            _spawn_poll_for_source(
                source_key, sync_timeout_s=settings["poll_wait_timeout_s"]
            )
            refreshed = _read_cache().get("providers", {}).get(source_key)
            if isinstance(refreshed, dict) and _fresh(
                refreshed, settings["max_cache_age_s"], _now_utc()
            ):
                record = refreshed
        if settings.get("refresh_every_request", True) and (
            (record or {}).get("fetched_at") == previous_fetched
            and (record or {}).get("polled_at") == previous_polled
        ):
            # m4: confirmation is (fetched_at, polled_at) — polled_at is
            # stamped by the poller on every completed fetch, including one
            # landing within the same second as the previous fetch.
            return _Decision(False, "Usage guard: a new usage snapshot could not be verified; the request was not sent.", "refresh not confirmed")
        if not _fresh(record, settings["max_cache_age_s"], _now_utc()):
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

    if record.get("unavailable_reason"):
        if settings["fail_open"] and config_ok:
            return _Decision(
                True, reason=str(record.get("unavailable_reason"))
            )
        return _Decision(
            False,
            _UNAVAILABLE_NOTICE.format(
                provider=provider, reason=record.get("unavailable_reason")
            ),
            reason=str(record.get("unavailable_reason")),
        )

    windows = record.get("windows")
    if record.get("ok") is not True or not isinstance(windows, list) or not windows:
        return _Decision(False, "Usage guard: usable allowance evidence is missing; the request was not sent.", "invalid allowance evidence")
    for window in windows:
        if not isinstance(window, dict):
            return _Decision(False, "Usage guard: malformed allowance window; the request was not sent.", "invalid window")
        used = window.get("used_percent")
        reset = window.get("reset_at")
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not math.isfinite(used) or used < 0
                or (reset is not None and _parse_ts(reset) is None)):
            return _Decision(False, "Usage guard: malformed allowance window; the request was not sent.", "invalid window")

    if source_key == "anthropic" and not record.get("raw_present") and not raw_warning_done():
        # M2: stock upstream core does not attach raw to Anthropic snapshots,
        # so the extra raw-window ingestion (seven_day_* buckets, limits[])
        # has nothing to read. Classic windows still gate; say so once.
        LOG.warning(
            "usage-guard: the Anthropic usage snapshot has no raw payload in this Hermes "
            "build; only the classic windows are gated. Raw-window gating requires a core "
            "that passes raw=payload in _fetch_anthropic_account_usage."
        )
        _mark_raw_warning()

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


def _spawn_poll_for_source(source_key: str, sync_timeout_s: float = 0.0) -> None:
    """Best-effort: start one bounded fetch of core's fetcher.

    The core import happens inside the worker thread — if the core module is
    absent the poll records ``fetch-error`` and the guard keeps its on-disk
    policy instead of crashing the request.

    With ``sync_timeout_s`` > 0 the caller waits (bounded) for the fetch so a
    fail-closed decision can be made against fresh data rather than stale
    data. The wait is capped well below a turn budget; on timeout the stale
    snapshot policy still applies.
    """

    def _fetch() -> Any:
        from agent.account_usage import fetch_account_usage as core_fetch

        return core_fetch(source_key)

    _spawn_poll(source_key, _fetch, sync_timeout_s=sync_timeout_s)


# ── MoA (virtual provider) slots ────────────────────────────────────────────

def _normalize_moa_config(moa_raw: Any) -> Any:
    """Core's MoA normalizer, imported lazily so the guard never vendors a copy."""
    from hermes_cli.moa_config import normalize_moa_config

    return normalize_moa_config(moa_raw or {})


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
