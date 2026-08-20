"""Host-existence gate + reconcile for tenant resolution. See deploy/resolve_gate_design.md.

`tres:hosts` (Redis SET, tenant_resolve db) holds every valid hostname; its EXISTENCE is
the consistency flag. Consulted on a positive-cache MISS:
  MEMBER    → known host → resolve from DB (cold-fill)
  NONMEMBER → unknown host → reject WITHOUT a DB hit
  UNKNOWN   → SET absent / Redis error → fail-open (caller resolves under fill_cap)

Keys (raw, tenant_resolve Redis). They use the `treg:` prefix — DISTINCT from the
django_redis snapshot namespace `<KEY_PREFIX>:<version>:*` (i.e. `tres:1:*`) — so the two
never overlap under a SCAN/`delete_pattern`, and are visually unambiguous:
  treg:hosts        the SET
  treg:hosts:new    reconcile's build target (RENAME'd into place)
  treg:hosts:dirty  mutation counter (dirty-recheck)
  treg:warming      single-writer lock (redis-py Lock: unique token, fenced release)
  treg:warm_pending enqueue-coalescing marker (self-expiring; set-only, never deleted)

Maintained by Domain signals (SADD/SREM + arm) and the reconcile task (force-rebuild).
Everything is gated by TENANT_REGISTRY_WARM_ENABLED / _GATE_ENABLED — a no-op until on.
"""
import logging

from redis.exceptions import LockError, RedisError

from . import flags
from .cache import resolve_cache
from .config import registry_cfg

logger = logging.getLogger(__name__)

HOSTS_KEY = "treg:hosts"
HOSTS_NEW_KEY = "treg:hosts:new"
DIRTY_KEY = "treg:hosts:dirty"
WARM_LOCK_KEY = "treg:warming"
WARM_PENDING_KEY = "treg:warm_pending"

# The dirty counter only needs to outlive one reconcile (sub-second) + the enqueue→run
# gap. Bound its lifetime so it can't leak forever on a volatile-ttl instance (which
# never evicts a no-TTL key). NX = set the TTL once per key-lifetime, don't slide it.
_DIRTY_TTL_SECONDS = 3600


class HostRegistry:
    # check() verdicts (identity sentinels)
    MEMBER = object()
    NONMEMBER = object()
    UNKNOWN = object()

    @property
    def warm_enabled(self):
        return flags.warm_enabled()

    @property
    def gate_enabled(self):
        # Fail-safe (GATE effective only with WARM) lives in flags.gate_enabled(): WARM is
        # the write side that builds tres:hosts; GATE-without-WARM would make every resolve
        # UNKNOWN -> fail-open + fill_cap-throttle legit traffic, so it is treated as OFF in
        # every process even if the tenants.E001 deploy check was skipped.
        return flags.gate_enabled()

    # ---- read (gate) ----
    def check(self, hostname):
        """MEMBER | NONMEMBER | UNKNOWN. UNKNOWN => fail-open (SET absent or Redis error)."""
        try:
            c = resolve_cache.get_redis_raw_client()
            pipe = c.pipeline()
            pipe.exists(HOSTS_KEY)
            pipe.sismember(HOSTS_KEY, hostname)
            exists, member = pipe.execute()
        except RedisError:
            return self.UNKNOWN                      # Redis error → fail-open (non-Redis bugs surface)
        if not exists:
            return self.UNKNOWN                      # SET absent → fail-open (+ trigger warm)
        return self.MEMBER if member else self.NONMEMBER

    # ---- incremental maintenance (Domain signals; WARM only) ----
    @staticmethod
    def _apply_membership(op, hostname):
        c = resolve_cache.get_redis_raw_client()
        if op == "sadd":
            # Guard без Lua: не створюємо SET інкрементно — лише reconcile (RENAME) може.
            # EXISTS→SADD не атомарно, але розійтись вони можуть ЛИШЕ через паралельний
            # reconcile, який або RENAME-ить свіжий повний SET (наш SADD додасть реальний
            # член), або (тільки на порожній БД) видаляє його — що неможливо, поки ми САМЕ
            # додаємо домен. Катастрофічний сценарій (брокер ліг, reconcile нема) має
            # EXISTS=False стабільно → ми коректно пропускаємо. Залишкова гонка
            # самолікується dirty-recheck-ом.
            if c.exists(HOSTS_KEY):
                c.sadd(HOSTS_KEY, hostname)
        else:
            c.srem(HOSTS_KEY, hostname)          # безумовний no-op на відсутньому
        pipe = c.pipeline()                       # dirty-bump: pipeline, не транзакція
        pipe.incr(DIRTY_KEY)
        pipe.expire(DIRTY_KEY, _DIRTY_TTL_SECONDS, nx=True)
        pipe.execute()

    def add(self, hostname):
        if not self.warm_enabled:
            return
        try:
            self._apply_membership("sadd", hostname)
        except RedisError:
            logger.warning("host_registry.add failed for %r", hostname, exc_info=True)

    def remove(self, hostname):
        if not self.warm_enabled:
            return
        try:
            self._apply_membership("srem", hostname)
        except RedisError:
            logger.warning("host_registry.remove failed for %r", hostname, exc_info=True)

    def arm(self):
        """Dead-man switch: give treg:hosts a short TTL so a failed follow-up reconcile
        self-heals (key expires → gate fails open → re-warm on next miss)."""
        if not self.warm_enabled:
            return
        ttl = registry_cfg.HOSTS_ARM_SECONDS
        try:
            resolve_cache.get_redis_raw_client().expire(HOSTS_KEY, ttl)
        except RedisError:
            logger.warning("host_registry.arm failed", exc_info=True)

    # ---- on-demand warm trigger ----
    def trigger_warm(self):
        """Enqueue a reconcile, coalescing bursts via a short SELF-EXPIRING NX marker
        (`tres:warm_pending`). One-sided lifecycle: only set-with-expiry here, never
        deleted elsewhere — so a lost broker publish self-heals when the marker expires,
        and no other writer can clear a marker it didn't set. Best-effort. Correctness of
        mid-run mutations is handled by the reconcile dirty-recheck, not this marker."""
        if not self.warm_enabled:
            return
        try:
            c = resolve_cache.get_redis_raw_client()
            ttl = registry_cfg.WARM_PENDING_SECONDS
            if c.set(WARM_PENDING_KEY, "1", nx=True, ex=ttl):
                from tenants.tasks import reconcile_host_registry_task
                reconcile_host_registry_task.delay()
        except Exception:
            # Deliberately broad (not just RedisError): this spans a Redis SET AND a broker
            # publish (.delay()), whose failures aren't RedisError. It's best-effort and on
            # the request path — a down broker must never break the request.
            logger.warning("host_registry.trigger_warm failed", exc_info=True)

    # ---- single-writer entry point (used by the celery task / mgmt command) ----
    def run_locked(self):
        """Acquire the tres:warming lock, reconcile, release. Returns the number of
        positives written, or None if another writer holds the lock (or WARM is off).

        Uses redis-py's Lock: a unique token + fenced release (release() deletes only if
        the token still matches) — so a run whose lock expired mid-reconcile can never
        delete a later writer's lock and let two reconciles overlap. `timeout` is the
        auto-release TTL (crash safety); blocking=False → skip if another writer holds it."""
        if not self.warm_enabled or not resolve_cache.redis_alive():
            return None
        c = resolve_cache.get_redis_raw_client()
        lock_ttl = registry_cfg.WARM_LOCK_SECONDS
        lock = c.lock(WARM_LOCK_KEY, timeout=lock_ttl)
        if not lock.acquire(blocking=False):
            return None                              # another writer is reconciling
        try:
            return self.reconcile()
        finally:
            try:
                lock.release()                       # fenced: raises if no longer ours
            except LockError:
                # Lock expired mid-reconcile (slower than lock_ttl) → not ours to release.
                # Safe to ignore: a later writer may now hold it; we must not touch theirs.
                logger.warning("host_registry: warm lock expired before release", exc_info=True)
            # NB: tres:warm_pending is intentionally NOT cleared here — it self-expires
            # (see trigger_warm). Clearing it would let this run wipe a marker a LATER
            # trigger set, and the marker never gated correctness (dirty-recheck does).

    # ---- reconcile (force-rebuild; the warm task body, single writer) ----
    def reconcile(self):
        """Force-rebuild `tres:hosts` + positive snapshots from the DB. The caller holds
        the tres:warming lock. Positive snapshots are FORCE-overwritten in place with fresh
        data (ttl_by_status) via put_many — the 5s hold is NOT respected here (reconcile is
        the authoritative single writer that just read the DB; the hold only guards the
        resolve-path nx race, and mid-build races are caught by dirty-recheck + orphan-sweep).
        The SET is built in tres:hosts:new and swapped in with an atomic RENAME; orphan-sweep
        drops stale positives; re-run if a mutation landed mid-build (dirty-recheck)."""
        if not self.warm_enabled or not resolve_cache.redis_alive():
            return 0
        c = resolve_cache.get_redis_raw_client()
        n, db_hosts = 0, set()
        for _ in range(3):                           # bounded re-runs on concurrent mutations
            before = c.get(DIRTY_KEY)
            n, db_hosts = self._rebuild_once(c)
            if c.get(DIRTY_KEY) == before:
                break
        resolve_cache.sweep_orphans(db_hosts)        # ONCE, after the SET is final (cache owns layout)
        return n

    _REBUILD_CHUNK = 2000

    @staticmethod
    def _rebuild_once(c):
        from tenants.models import Domain
        c.delete(HOSTS_NEW_KEY)
        db_hosts, n, batch = set(), 0, []

        def flush():
            nonlocal n
            if not batch:
                return
            c.sadd(HOSTS_NEW_KEY, *(dm.domain for dm in batch))                 # 1 round-trip
            n += resolve_cache.put_many((dm.domain, dm.tenant) for dm in batch)  # ≤ #distinct-TTL
            batch.clear()

        chunk = HostRegistry._REBUILD_CHUNK
        for d in Domain.objects.select_related("tenant__shard").iterator(chunk_size=chunk):
            db_hosts.add(d.domain)
            batch.append(d)
            if len(batch) >= chunk:
                flush()
        flush()

        if c.exists(HOSTS_NEW_KEY):
            c.rename(HOSTS_NEW_KEY, HOSTS_KEY)       # atomic swap; no TTL => flag present
        else:
            logger.warning("host_registry: rebuilt treg:hosts to EMPTY (no domains) — gate → fail-open")
            c.delete(HOSTS_KEY)                      # nothing valid → flag absent (fail-open)
        return n, db_hosts


host_registry = HostRegistry()
