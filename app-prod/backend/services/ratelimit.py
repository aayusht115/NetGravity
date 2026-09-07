"""
NetGravity — Request rate limiting
==================================
Two endpoint families need a limit for different reasons.

**Credential endpoints** (`/api/auth/login`, `/signup`, `/password/reset`) are
guessed at. The account lockout in `security.py` handles a sustained attack on
ONE account; this handles the other shape — a spray across many accounts from
one source, which never trips a per-account counter.

**Solve endpoints** (`/api/kpis/*`, `/api/scenarios/simulate`, `/orchestrator/*`)
are expensive. One caller can occupy every worker with MILP solves and the
platform stops responding for everyone else without any malice being involved.

Design
------
A fixed-window counter per (bucket, client), **held in the database and shared
by every worker**.

It used to be held in process memory, and that was stated as a known limit: with
N workers a caller got N budgets. The trouble with that trade is which way it
fails. The limit exists to stop a caller occupying every worker with MILP
solves — and the moment you add workers to survive that load, the limit
loosens by exactly the factor you added. The number a deployment advertised was
never the number it enforced, and the gap grew with the deployment.

The counter is now one row updated by one atomic statement, so a burst arriving
simultaneously on four workers is counted four times against one budget rather
than once each against four. That costs one round trip per limited request —
paid only on credential and solve endpoints, which are the expensive ones
anyway, and set against a MILP solve it is not measurable.

Redis would be faster and is what most deployments reach for. It is not used
here because it would be a second datastore to run, secure, back up and monitor
for a table with one small row per active caller, and PostgreSQL is already
all four of those things. `_Store` is the seam if that trade ever changes.

If the database is unreachable the limiter falls back to a per-process window
rather than to no limit at all — a degraded limit is still a limit, and an
outage of the store must not become an open door.

The client is identified by the authenticated user where there is one, and by
the peer address otherwise — with `X-Forwarded-For` honoured ONLY when
`NETGRAVITY_TRUSTED_PROXY=1`, because a header anyone can set is a way to evade
the limit, not a way to enforce it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from functools import wraps
from typing import Callable, Dict, Optional, Tuple

from flask import g, jsonify, request

logger = logging.getLogger(__name__)

#: How often a worker sweeps windows nobody is in any more.
_SWEEP_INTERVAL_SECONDS = 300.0


def configured_limit(bucket: str, default: int) -> int:
    """
    A bucket's limit, overridable by environment.

    Every one of these numbers was a hardcoded literal at its decorator, which
    was tolerable while each worker had its own budget and the effective limit
    was N times whatever was written. Sharing the counter made the written
    number the real number — and `auth.signup` at 10 per hour per address then
    meant a team behind one office NAT could register ten people an hour and no
    more. That is the correct kind of thing for an operator to set, so it is
    settable: `NETGRAVITY_RATELIMIT_AUTH_SIGNUP=60`.
    """
    key = "NETGRAVITY_RATELIMIT_" + bucket.upper().replace(".", "_")
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ratelimit.bad_override %s=%r ignored", key, raw)
        return default
    if value < 1:
        logger.warning("ratelimit.bad_override %s=%r must be >= 1; ignored", key, raw)
        return default
    logger.info("ratelimit.override bucket=%s limit=%d (was %d)", bucket, value, default)
    return value


class _MemoryStore:
    """
    Per-process windows. The fallback, and what tests without a database use.

    Retained rather than deleted: it is what the limiter degrades to when the
    shared store is unreachable, and losing the limit entirely at that moment
    would be worse than losing its exactness.
    """

    shared = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: Dict[Tuple[str, str], Tuple[float, int]] = defaultdict(
            lambda: (0.0, 0))
        self._last_sweep = time.time()

    def bump(self, bucket: str, client: str, now: float,
             window_seconds: float) -> Tuple[int, float]:
        key = (bucket, client)
        with self._lock:
            self._sweep(now, window_seconds)
            started, count = self._windows[key]
            if now - started >= window_seconds:
                started, count = now, 0
            count += 1
            self._windows[key] = (started, count)
        return count, started

    def _sweep(self, now: float, window_seconds: float) -> None:
        """Drop windows nobody is in any more. Called under the lock."""
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        stale = [k for k, (started, _) in self._windows.items()
                 if now - started > max(window_seconds, 3600)]
        for k in stale:
            del self._windows[k]

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()


class _DatabaseStore:
    """Windows every worker shares, incremented by one atomic upsert."""

    shared = True

    def __init__(self) -> None:
        self._last_sweep = 0.0

    def bump(self, bucket: str, client: str, now: float,
             window_seconds: float) -> Tuple[int, float]:
        from app.backend.services import persistence
        hits, started = persistence.bump_rate_limit_window(
            bucket, client, now, window_seconds)
        if now - self._last_sweep > _SWEEP_INTERVAL_SECONDS:
            self._last_sweep = now
            try:
                persistence.purge_rate_limit_windows(now - max(window_seconds, 3600))
            except Exception as exc:  # noqa: BLE001 — housekeeping, never the request
                logger.warning("ratelimit.sweep_failed error=%s", exc)
        return hits, started

    def clear(self) -> None:
        from app.backend.services import persistence
        persistence.clear_rate_limit_windows()


class RateLimiter:
    """Fixed-window counters, one window per (bucket, client)."""

    def __init__(self) -> None:
        self._store: object = _MemoryStore()
        self._fallback = _MemoryStore()
        self._degraded = False

    def use_shared_store(self) -> None:
        """
        Switch to the counter every worker shares.

        Called once the database is known to be reachable and migrated, rather
        than at import: a limiter that raised on a missing table would take
        down the very endpoints it protects.
        """
        self._store = _DatabaseStore()
        self._degraded = False

    def use_memory_store(self) -> None:
        self._store = _MemoryStore()

    @property
    def is_shared(self) -> bool:
        return bool(getattr(self._store, "shared", False)) and not self._degraded

    def check(self, bucket: str, client: str, *, limit: int,
              window_seconds: float) -> Tuple[bool, int, float]:
        """
        Count one request. Returns `(allowed, remaining, retry_after)`.

        The window is fixed rather than sliding: a sliding window needs the
        timestamp of every request in it, which is unbounded storage for an
        endpoint under attack — the exact condition it exists for.
        """
        now = time.time()
        try:
            count, started = self._store.bump(bucket, client, now, window_seconds)
            if self._degraded:
                self._degraded = False
                logger.info("ratelimit.store_recovered bucket=%s", bucket)
        except Exception as exc:  # noqa: BLE001
            # A store that cannot be reached must not become an open door.
            if not self._degraded:
                logger.error(
                    "ratelimit.store_unavailable falling back to per-process "
                    "counters error=%s", exc)
            self._degraded = True
            count, started = self._fallback.bump(bucket, client, now, window_seconds)

        if count > limit:
            return False, 0, max(0.0, window_seconds - (now - started))
        return True, limit - count, 0.0

    def reset(self) -> None:
        """For tests, so one test's attempts do not exhaust another's budget."""
        self._fallback.clear()
        try:
            self._store.clear()
        except Exception as exc:  # noqa: BLE001 — a reset must never fail a test run
            logger.debug("ratelimit.reset_skipped error=%s", exc)


limiter = RateLimiter()


def client_identity() -> str:
    """
    Who is being limited.

    The authenticated user when there is one — so a shared office IP does not
    put everyone in one bucket — and the peer address otherwise.

    `X-Forwarded-For` is honoured ONLY behind a trusted proxy. Taking it on
    trust by default means any caller sets their own identity and the limit
    stops existing.
    """
    user = getattr(g, "current_user", None)
    if user is not None:
        return f"user:{user.user_id}"
    if os.environ.get("NETGRAVITY_TRUSTED_PROXY", "0") == "1":
        forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded:
            return f"ip:{forwarded}"
    return f"ip:{request.remote_addr or 'unknown'}"


def rate_limit(bucket: str, *, limit: int, window_seconds: float) -> Callable:
    """
    Refuse a caller past `limit` requests in `window_seconds`.

    Returns 429 with `Retry-After`, so a well-behaved client backs off instead
    of retrying into the wall.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if os.environ.get("NETGRAVITY_DISABLE_RATE_LIMIT") == "1":
                return fn(*args, **kwargs)
            client = client_identity()
            # Resolved per request, not per import, so an operator's override
            # takes effect without the decorator having been re-evaluated.
            effective = configured_limit(bucket, limit)
            allowed, remaining, retry_after = limiter.check(
                bucket, client, limit=effective, window_seconds=window_seconds)
            if not allowed:
                logger.warning("ratelimit.refused bucket=%s client=%s", bucket, client)
                response = jsonify({"error": {
                    "code": "RATE_LIMITED",
                    "message": (
                        f"Too many requests. This endpoint allows {effective} per "
                        f"{int(window_seconds)} seconds; try again in "
                        f"{int(retry_after) + 1}s."),
                }})
                response.status_code = 429
                response.headers["Retry-After"] = str(int(retry_after) + 1)
                return response
            response = fn(*args, **kwargs)
            try:
                response_obj = response[0] if isinstance(response, tuple) else response
                if hasattr(response_obj, "headers"):
                    response_obj.headers["X-RateLimit-Limit"] = str(effective)
                    response_obj.headers["X-RateLimit-Remaining"] = str(remaining)
            except Exception:  # noqa: BLE001 — headers are a courtesy, never a failure
                pass
            return response

        return wrapper

    return decorator
