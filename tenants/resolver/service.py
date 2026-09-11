"""Tenant-resolve service facade — the single entry point the middleware calls.

Encapsulates the WHOLE resolve policy: positive/negative snapshot cache, the host gate
(member / non-member / unknown), fill_cap throttling, single-flight coalescing, and the
fail-open fallback. The HTTP middleware only supplies a DB-resolver closure and the
"not found" exception class; it knows nothing about the cache/gate mechanics.
"""
import logging
import time
from collections.abc import Callable

from django.db import OperationalError
from redis.exceptions import RedisError

from .cache import resolve_cache
from .registry import host_registry
from .snapshot import TenantSnapshot
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
_shed_last = 0.0
_shed_suppressed = 0


class ResolveDeferred(Exception):
    """Resolution was DECLINED to shed load, not answered.

    Raised on the gate's flag-absent (UNKNOWN) branch when fill_cap is exhausted: the host
    registry is unavailable, so a member cannot be told from a stranger, and the DB budget
    for that ambiguity is spent. The host may be perfectly valid - so this must NOT surface
    as "no such tenant". The caller maps it to a retryable 503.

    Deliberately NOT a subclass of the caller's not_found: "does not exist" and "declined to
    look" are different answers, and only one of them is a fact about the host.
    """


def _log_cache_fail(hostname: str) -> None:
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


def _log_cache_bug(hostname: str) -> None:
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


def _log_shed(hostname: str) -> None:
    """Load-shed on the flag-absent branch. Sustained shedding means the registry SET is
    missing (or its Redis is down) under real traffic - an operational signal, not a
    per-request event, so it is rate-limited like the two helpers above. No exc_info: there
    is no exception here, this is a decision."""
    global _shed_last, _shed_suppressed
    now = time.monotonic()
    if now - _shed_last >= _FAIL_LOG_EVERY:
        extra = f" ({_shed_suppressed} similar suppressed)" if _shed_suppressed else ""
        logger.warning("tenant resolve DEFERRED (registry flag absent, fill_cap exhausted; "
                       "e.g. %r) -> retryable 503%s", hostname, extra)
        _shed_last, _shed_suppressed = now, 0
    else:
        _shed_suppressed += 1


def resolve(hostname: str, db_resolver: Callable[[], TenantSnapshot],
            not_found: type[Exception]) -> TenantSnapshot:
    """Resolve a Host to a Tenant. ``db_resolver()`` is the authoritative DB lookup
    (returns a Tenant or raises ``not_found``). Returns the Tenant, or raises
    ``not_found`` (unknown host) / ``ResolveDeferred`` (declined to look — load-shed) /
    ``OperationalError`` (DB down). A cache-layer FAILURE degrades to a plain DB resolve
    (fail-open) — the cache is only ever an optimization and the DB resolve is the correct
    answer. A ``ResolveDeferred`` is the one deliberate NON-degrade: shedding load is the
    point, so it must reach the caller instead of falling through to the DB. EXPECTED infra
    failures (RedisError) log quietly; UNEXPECTED errors (a bug in the cache/gate path)
    still fail open but log LOUD (ERROR) so they don't hide behind 'it just got slower'."""
    # Skip the whole cache/gate machinery only when NEITHER is in play. The two terms are
    # NOT independent: gate_enabled ⇒ warm_enabled ⇒ enabled (flags.gate_enabled requires
    # WARM; cache.enabled counts WARM), so the second one can never decide the outcome — it
    # is kept as a readable statement of that intent, not as defence-in-depth. Both links of
    # the implication are pinned by tests (test_resolve_gate: GATE⇒WARM, WARM⇒enabled), so
    # neither can be broken silently.
    if not resolve_cache.enabled and not host_registry.gate_enabled:
        return db_resolver()                        # nothing in play → direct DB (today's behavior)
    try:
        return _via_cache(hostname, db_resolver, not_found)
    except not_found:
        raise                                       # real "no tenant for this host"
    except ResolveDeferred:
        raise                                       # deliberate load-shed — must NOT fall
                                                    # through to the DB in the branches below
    except OperationalError:
        raise                                       # DB down — surface; don't retry a dead DB
    except RedisError:
        _log_cache_fail(hostname)                   # EXPECTED infra → quiet WARNING, fail-open
        return db_resolver()
    except Exception:
        _log_cache_bug(hostname)                    # UNEXPECTED bug → LOUD ERROR, still fail-open
        return db_resolver()


def _via_cache(hostname: str, db_resolver: Callable[[], TenantSnapshot],
               not_found: type[Exception]) -> TenantSnapshot:
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
                # NOT not_found: we never established that this host is unknown, we declined
                # to look. Surfacing it as 404 would tell a legitimate tenant's users their
                # workspace is gone. The caller turns this into a retryable 503.
                _log_shed(hostname)
                raise ResolveDeferred(hostname)
    # MEMBER, or UNKNOWN within budget → resolve (coalesced across concurrent callers)
    return single_flight(hostname, lambda: _fill(hostname, db_resolver, not_found))


def _fill(hostname: str, db_resolver: Callable[[], TenantSnapshot],
          not_found: type[Exception]) -> TenantSnapshot:
    try:
        tenant = db_resolver()
    except not_found:
        resolve_cache.store_miss(hostname)
        raise
    resolve_cache.put(hostname, tenant)
    return tenant
