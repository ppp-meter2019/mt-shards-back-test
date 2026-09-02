"""Tenant-resolve service facade — the single entry point the middleware calls.

Encapsulates the WHOLE resolve policy: positive/negative snapshot cache, the host gate
(member / non-member / unknown), fill_cap throttling, single-flight coalescing, and the
fail-open fallback. The HTTP middleware only supplies a DB-resolver closure and the
"not found" exception class; it knows nothing about the cache/gate mechanics.
"""
import logging
import time

from django.db import OperationalError
from redis.exceptions import RedisError

from .cache import resolve_cache
from .registry import host_registry
from .throttle import fill_cap, single_flight

logger = logging.getLogger(__name__)

# Rate-limit for the fail-open path: a sustained cache-path failure must not emit one
# traceback per request. First hit (then at most once per _FAIL_LOG_EVERY) logs a full
# traceback plus how many similar were suppressed since. Per-process; the unlocked shared
# state is a benign race (at worst a double log / slight miscount) — fine for a log throttle.
_FAIL_LOG_EVERY = 30.0
_fail_last = 0.0
_fail_suppressed = 0
_bug_last = 0.0
_bug_suppressed = 0


def _log_cache_fail(hostname):
    """EXPECTED infra failure (Redis down/slow): quiet-ish WARNING, rate-limited. Fail-open is
    correct — the DB resolve returns the right tenant."""
    global _fail_last, _fail_suppressed
    now = time.monotonic()
    if now - _fail_last >= _FAIL_LOG_EVERY:
        extra = f" ({_fail_suppressed} similar suppressed)" if _fail_suppressed else ""
        logger.warning("tenant resolve cache path failed (e.g. %r); DB fallback%s",
                       hostname, extra, exc_info=True)
        _fail_last, _fail_suppressed = now, 0
    else:
        _fail_suppressed += 1


def _log_cache_bug(hostname):
    """UNEXPECTED error = a BUG in the cache/gate path (infra is already handled: the wrapped
    cache masks Redis-down to a miss, and the gate catches RedisError). We STILL fail open —
    the DB resolve is correct, so a cache-layer bug must not 500 the request — but we log LOUD
    (ERROR, rate-limited) so the bug cannot masquerade as merely 'slower'."""
    global _bug_last, _bug_suppressed
    now = time.monotonic()
    if now - _bug_last >= _FAIL_LOG_EVERY:
        extra = f" ({_bug_suppressed} similar suppressed)" if _bug_suppressed else ""
        logger.error("tenant resolve cache path BUG (unexpected error resolving %r); DB "
                     "fallback%s", hostname, extra, exc_info=True)
        _bug_last, _bug_suppressed = now, 0
    else:
        _bug_suppressed += 1


def resolve(hostname, db_resolver, not_found):
    """Resolve a Host to a Tenant. ``db_resolver()`` is the authoritative DB lookup
    (returns a Tenant or raises ``not_found``). Returns the Tenant, or raises
    ``not_found`` (unknown host) / ``OperationalError`` (DB down). A cache-layer failure
    degrades to a plain DB resolve (fail-open) — the cache is only ever an optimization and
    the DB resolve is the correct answer. EXPECTED infra failures (RedisError) log quietly;
    UNEXPECTED errors (a bug in the cache/gate path) still fail open but log LOUD (ERROR) so
    they don't hide behind 'it just got slower'."""
    if not resolve_cache.enabled and not host_registry.gate_enabled:
        return db_resolver()                        # cache AND gate off → direct DB (today's behavior)
    try:
        return _via_cache(hostname, db_resolver, not_found)
    except not_found:
        raise                                       # real "no tenant for this host"
    except OperationalError:
        raise                                       # DB down — surface; don't retry a dead DB
    except RedisError:
        _log_cache_fail(hostname)                   # EXPECTED infra → quiet WARNING, fail-open
        return db_resolver()
    except Exception:
        _log_cache_bug(hostname)                    # UNEXPECTED bug → LOUD ERROR, still fail-open
        return db_resolver()


def _via_cache(hostname, db_resolver, not_found):
    snap = resolve_cache.get_snapshot(hostname)
    if snap is resolve_cache.NEG:
        raise not_found(hostname)                   # cached miss — no DB
    if snap is not resolve_cache.MISS:
        return snap                                 # positive hit
    # --- MISS ---
    if host_registry.gate_enabled:
        verdict = host_registry.check(hostname)
        if verdict is host_registry.NONMEMBER:
            raise not_found(hostname)               # unknown host → reject, no DB, no store_miss
        if verdict is host_registry.UNKNOWN:        # SET absent / Redis error → fail-open under cap
            host_registry.trigger_warm()
            if not fill_cap.allow():
                raise not_found(hostname)
    # MEMBER, or UNKNOWN within budget → resolve (coalesced across concurrent callers)
    return single_flight(hostname, lambda: _fill(hostname, db_resolver, not_found))


def _fill(hostname, db_resolver, not_found):
    try:
        tenant = db_resolver()
    except not_found:
        resolve_cache.store_miss(hostname)
        raise
    resolve_cache.put(hostname, tenant)
    return tenant
