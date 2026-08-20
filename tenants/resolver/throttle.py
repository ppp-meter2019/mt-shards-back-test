"""Throttle helpers for tenant resolution: fill_cap + single_flight. See
deploy/resolve_gate_design.md. NB: these do NOT make the gate decision (that is
host_registry + service) — they only bound DB load on the resolve path.

fill_cap (fill_cap.allow): caps DB resolves ONLY on the service's flag-absent branch
(cold / flush / warm-in-progress), where member vs unknown can't be told apart — so a
flood can't stampede the shared `default` pool. Global fixed-window counter in the
tenant_resolve Redis; falls back to a per-pod token bucket when that Redis op fails
(protect the pool over new-domain freshness — never open the floodgates).

single_flight: coalesces concurrent DB resolves of the SAME host within a process, so a
burst (e.g. a cache flush) issues one query per distinct host, not one per request.
No-op at concurrency 1; dedups at concurrency > 1.
"""
import threading
import time

from redis.exceptions import RedisError

from .cache import resolve_cache
from .config import resolve_cfg


class _LocalBucket:
    """Per-pod token bucket — the fallback when the global Redis counter is unreachable."""

    def __init__(self):
        self._tokens = 0.0
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def allow(self, rate):
        cap = max(1.0, float(rate))
        with self._lock:                       # uncontended at concurrency 1
            now = time.monotonic()
            self._tokens = min(cap, self._tokens + (now - self._ts) * rate)
            self._ts = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class FillCap:
    """Global (cluster-wide) FIXED-WINDOW rate limiter for flag-absent DB resolves.

    Fixed window = one counter per calendar second. Known trade-off: at a window boundary up
    to ~2×limit can pass in a short real interval (limit at t=…999ms + limit at t+1=…001ms),
    so "N/sec" holds per calendar second, not per arbitrary sliding second. Accepted — this
    is a coarse DoS backstop (keep DB load O(limit), not an exact QoS limiter), it fires only
    on the transient flag-absent branch, and the local fallback (_LocalBucket) is a smooth
    token bucket. A precise sliding window (ZSET log / weighted counter) would cost more Redis
    ops/memory per hot reject for no real benefit here."""

    _local = _LocalBucket()

    @property
    def _limit(self):
        return resolve_cfg.FILLCAP_PER_SEC

    @property
    def _local_rate(self):
        return resolve_cfg.FILLCAP_LOCAL_PER_SEC

    def allow(self):
        """True => a flag-absent DB resolve may proceed; False => reject fast (no DB)."""
        try:
            c = resolve_cache.get_redis_raw_client()
            key = f"treg:probe:{int(time.time())}"   # 1-second fixed window, cluster-global
            n = c.incr(key)
            c.expire(key, 2, nx=True)                # set TTL once (NX = never extend the window)
            return n <= self._limit
        except RedisError:
            # Global counter unreachable → do NOT open the floodgates: degrade to a
            # conservative per-pod bucket so the shared pool stays protected.
            return self._local.allow(self._local_rate)


fill_cap = FillCap()


# --- single-flight coalescing -------------------------------------------------
_inflight = {}
_inflight_lock = threading.Lock()
_UNSET = object()   # leader produced no value (aborted via control-flow / crash)


def single_flight(key, resolver, wait_timeout=5.0):
    """Run ``resolver()`` once per ``key`` across concurrent callers in THIS process.
    The leader runs it; followers wait and share its result or its (real) exception. If the
    leader is slower than ``wait_timeout`` a follower falls back to its own resolve (no
    deadlock).

    Only ``Exception`` is shared with followers. Control-flow exceptions (SystemExit /
    KeyboardInterrupt / GeneratorExit — e.g. a worker shutdown signal hitting the leader)
    are NOT caught: they propagate in the LEADER thread alone and must never be re-raised in
    unrelated follower request threads. The finally still fires (slot cleaned, followers
    woken); seeing no shared result/exc, followers self-resolve."""
    with _inflight_lock:
        slot = _inflight.get(key)
        leader = slot is None
        if leader:
            slot = _inflight[key] = {"event": threading.Event(), "result": _UNSET, "exc": None}
    if leader:
        try:
            slot["result"] = resolver()
        except Exception as exc:                # real failure → share; control-flow falls through
            slot["exc"] = exc
        finally:
            with _inflight_lock:
                _inflight.pop(key, None)
            slot["event"].set()                 # control-flow leaves result=_UNSET, exc=None
    elif not slot["event"].wait(timeout=wait_timeout):
        return resolver()                       # leader too slow → own resolve
    if slot["exc"] is not None:
        raise slot["exc"]
    if slot["result"] is _UNSET:                # leader aborted before a value → self-resolve
        return resolver()
    return slot["result"]
