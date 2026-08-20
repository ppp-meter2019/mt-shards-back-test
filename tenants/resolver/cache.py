"""Tenant-resolution cache — a single service object owning all CACHES["tenant_resolve"]
mechanics: markers, TTLs, dump/load, the nx+tombstone coherency protocol, invalidation
(one host / hosts / tenant / tenants / ids / names / all) and warm-up.

Read/write primitives (get_snapshot / put / store / store_miss / sweep_orphans) are
driven by the resolver service facade (tenants.resolver.service), NOT the middleware.
Fail-open (degrade to a DB resolve on any cache error) is the service's concern;
IGNORE_EXCEPTIONS masks Redis-down as a miss. redis_alive() bypasses that mask for
management commands and raise_on_error callers.
"""
import logging

from django.core.cache import caches
from django.db import transaction

from . import flags
from .config import resolve_cfg
from .markers import NEGATIVE, TOMBSTONE

logger = logging.getLogger(__name__)


class CacheUnavailable(RuntimeError):
    """Raised when raise_on_error=True and the tenant_resolve Redis is unreachable."""


class TenantResolveCache:
    # get_snapshot() result sentinels (distinct from the string markers in the cache)
    MISS = object()
    NEG = object()

    # Logical sub-namespace for per-host snapshot keys: physical key is
    # `<KEY_PREFIX>:<version>:host-snap:<host>` (e.g. tres:1:host-snap:<host>). Keeping
    # snapshots under a dedicated prefix means the SCAN/sweep operate ONLY on host snapshots
    # — any future auxiliary/service key stored via this Django cache (a different logical
    # prefix) or via the raw client (the gate's treg:* keys) is structurally excluded, so it
    # can never be mistaken for a host and swept. All host↔key mapping goes through _snap_key().
    _SNAP_PREFIX = "host-snap:"

    def __init__(self, cache=None):
        self._cache = cache            # DI for tests; else resolved lazily

    @property
    def cache(self):
        # DI (tests) wins; otherwise resolve per access. caches[...] is a cheap
        # thread-local lookup Django already caches per thread AND recycles on
        # request_finished — so we neither share one instance across threads nor
        # hold a closed connection across requests.
        return self._cache if self._cache is not None else caches["tenant_resolve"]

    def get_redis_raw_client(self, write=True):
        """The underlying redis-py client for CACHES['tenant_resolve'], bypassing the
        django-cache wrapper. Use it for ops the Django cache API doesn't expose —
        SET NX/EX, INCR, EXPIRE, SISMEMBER, SCAN, pipeline, ping. Unlike the wrapped
        cache, errors here PROPAGATE (no IGNORE_EXCEPTIONS masking), so callers must
        handle them explicitly (fail-open). Single source for all raw-client access."""
        return self.cache.client.get_client(write=write)

    # ---- config (read live so override_settings works) ----
    @property
    def _pos_ttl(self):
        return resolve_cfg.POSITIVE_CACHE_SECONDS

    def ttl_for_status(self, status):
        """Positive-snapshot TTL by tenant status when the registry WARM stage is on.
        Returns None for statuses mapped to no-expiry (e.g. ACTIVE); falls back to the
        flat _pos_ttl for statuses not listed. Used by store() and the reconcile task."""
        mapping = resolve_cfg.WARM_TTL_BY_STATUS
        return mapping[status] if status in mapping else self._pos_ttl

    @property
    def _neg_ttl(self):
        return resolve_cfg.MISS_CACHE_SECONDS

    @property
    def _hold(self):
        return resolve_cfg.HOLD_SECONDS

    @property
    def warm_enabled(self):
        return flags.warm_enabled()

    @property
    def enabled(self):
        # WARM counts too: under WARM positives are written with ttl_by_status, so the flat
        # _pos_ttl can be 0 while the cache is fully in use. Without WARM here, the resolve
        # short-circuit in service.resolve() would skip a cache the reconcile keeps filled.
        return bool(self.warm_enabled or self._pos_ttl or self._neg_ttl)

    # ---- health (bypasses IGNORE_EXCEPTIONS) ----
    def redis_alive(self):
        try:
            return bool(self.get_redis_raw_client().ping())
        except Exception:
            return False

    def _ensure_alive(self, raise_on_error):
        if raise_on_error and not self.redis_alive():
            raise CacheUnavailable("tenant_resolve Redis is not reachable")

    # ---- serialization ----
    # Explicit allowlist of the fields a snapshot carries — NOT every model field. The
    # reconstructed request.tenant is used ONLY for routing (schema + shard) and the status
    # gate, so it carries just those (plus pks for FK coherence).
    # CONTRACT: never read any OTHER Tenant/Shard attribute off request.tenant — on a cache
    # HIT it would be the model default and diverge from a cache MISS (fresh from DB). Adding
    # a field here is a deliberate act: it puts that value into Redis (tenant_resolve, db2).
    # This is why company_name / description / last_error / timestamps stay OUT.
    _SNAPSHOT_FIELDS = {
        "tenant": ("id", "schema_name", "status", "shard_id"),
        "shard":  ("id", "alias"),
    }

    @classmethod
    def dump(cls, tenant):
        shard = tenant.shard
        return {
            "tenant": {f: getattr(tenant, f) for f in cls._SNAPSHOT_FIELDS["tenant"]},
            "shard":  {f: getattr(shard,  f) for f in cls._SNAPSHOT_FIELDS["shard"]},
        }

    @staticmethod
    def _build(model, values):
        fields = {f.attname for f in model._meta.fields}
        return model(**{k: v for k, v in values.items() if k in fields})

    @classmethod
    def load(cls, data):
        # Rebuild a read-only routing snapshot from _SNAPSHOT_FIELDS. Non-carried fields
        # take their model default — do NOT read them off request.tenant (see the CONTRACT
        # on _SNAPSHOT_FIELDS). Tolerant of legacy entries that still carry extra keys:
        # _build filters to real model fields, so old full snapshots load fine.
        from tenants.models import Shard, Tenant
        tenant = cls._build(Tenant, data["tenant"])
        shard = cls._build(Shard, data["shard"])
        shard.read_only = True
        tenant.shard = shard                 # real pk -> shard_id consistent; .alias needs no DB
        tenant.read_only = True
        return tenant

    # ---- key namespacing ----
    def _snap_key(self, hostname):
        """Logical cache key for a host snapshot (host → 'host-snap:<host>'). The single place that
        maps a hostname to its cache key; callers pass/receive bare hostnames."""
        return f"{self._SNAP_PREFIX}{hostname}"

    # ---- value classification (single source: what a raw cached value is) ----
    class _Kind:
        POSITIVE = "positive"   # a real snapshot payload (dict)
        NEG      = "neg"        # cached-miss marker
        HOLD     = "hold"       # tombstone (invalidation hold)
        MISS     = "miss"       # absent OR Redis-down (None)
        UNKNOWN  = "unknown"    # corrupt / unexpected type

    @classmethod
    def _classify(cls, cached):
        """Categorize a raw cached value. The ONE place that knows the on-wire shapes —
        get_snapshot, sweep and the bench all route through this."""
        if cached is None:            return cls._Kind.MISS
        if cached == TOMBSTONE:       return cls._Kind.HOLD
        if cached == NEGATIVE:        return cls._Kind.NEG
        if isinstance(cached, dict) and "tenant" in cached and "shard" in cached:
            return cls._Kind.POSITIVE          # has the shape load() understands
        return cls._Kind.UNKNOWN               # non-dict OR malformed dict → treated as a miss

    # ---- read primitives (used by middleware) ----
    def get_snapshot(self, hostname):
        """MISS (absent/tombstone/Redis-down/corrupt) | NEG (cached miss) | reconstructed Tenant."""
        cached = self.cache.get(self._snap_key(hostname))  # None on absent OR Redis error
        kind = self._classify(cached)
        if kind is self._Kind.POSITIVE:
            return self.load(cached)
        if kind is self._Kind.NEG:
            return self.NEG
        return self.MISS                          # MISS / HOLD / UNKNOWN → treat as a miss

    def _ttl_for(self, tenant, warm):
        """Positive-snapshot TTL for a tenant: ttl_by_status under WARM, else flat _pos_ttl."""
        return self.ttl_for_status(tenant.status) if warm else self._pos_ttl

    def put(self, hostname, tenant):
        """Single-item resolve-path writer (nx): write only if absent, so a hold marker
        (tombstone) is never overwritten and a slow resolver can't resurrect stale data.
        Returns True if it wrote. TTL: ttl_by_status under WARM (ACTIVE→None=no expiry),
        else flat _pos_ttl; a no-op when positive caching is off (legacy _pos_ttl=0, WARM
        off). The reconcile path uses put_many() (batched force-overwrite)."""
        warm = self.warm_enabled
        if not warm and not self._pos_ttl:
            return False
        self.cache.set(self._snap_key(hostname), self.dump(tenant),
                       self._ttl_for(tenant, warm), nx=True)
        return True

    def store(self, hostname, tenant):
        """Resolve-path fill (nx). Thin alias over put() — kept for existing callers."""
        self.put(hostname, tenant)

    def put_many(self, items):
        """Batched FORCE-write of positive snapshots — the reconcile path. `items` is an
        iterable of (hostname, tenant). Groups entries by TTL (ttl_by_status under WARM,
        else flat _pos_ttl) and issues ONE set_many per distinct TTL (django_redis pipelines
        each). Returns the number written. A no-op when positive caching is off.

        FORCE semantics: overwrites unconditionally — the 5s hold (tombstone) is NOT
        respected here. That is deliberate: reconcile is the authoritative single writer
        that just read the DB, so writing fresh data over a hold is correct; the hold only
        guards the resolve path's nx race (put/store), and the real mid-rebuild races are
        handled by the dirty-recheck + sweep_orphans, not by the hold."""
        warm = self.warm_enabled
        if not warm and not self._pos_ttl:
            return 0
        by_ttl = {}
        for hostname, tenant in items:
            by_ttl.setdefault(self._ttl_for(tenant, warm), {})[self._snap_key(hostname)] = self.dump(tenant)
        n = 0
        for ttl, batch in by_ttl.items():
            self.cache.set_many(batch, ttl)
            n += len(batch)
        return n

    def store_miss(self, hostname):
        if self._neg_ttl:
            self.cache.set(self._snap_key(hostname), NEGATIVE, self._neg_ttl, nx=True)

    # ---- invalidation ----
    def forget_hosts(self, hostnames, *, raise_on_error=False):
        self._ensure_alive(raise_on_error)   # checked first — honored even for an empty target
        hostnames = [h for h in hostnames if h]
        if not hostnames:
            return 0
        cache, hold = self.cache, self._hold

        keys = [self._snap_key(h) for h in hostnames]

        def _invalidate():
            if hold:
                cache.set_many({k: TOMBSTONE for k in keys}, hold)   # hold marker
            else:
                cache.delete_many(keys)                              # hold disabled

        transaction.on_commit(_invalidate)
        return len(hostnames)

    def forget_host(self, hostname, *, raise_on_error=False):
        return self.forget_hosts([hostname], raise_on_error=raise_on_error)

    def forget_tenant(self, tenant, *, raise_on_error=False):
        return self.forget_hosts(tenant.domains.values_list("domain", flat=True),
                          raise_on_error=raise_on_error)

    def forget_tenants(self, tenants, *, raise_on_error=False):
        from tenants.models import Domain
        return self.forget_hosts(
            Domain.objects.filter(tenant__in=tenants).values_list("domain", flat=True),
            raise_on_error=raise_on_error,
        )

    def forget_ids(self, ids, *, raise_on_error=False):
        from tenants.models import Domain
        return self.forget_hosts(
            Domain.objects.filter(tenant_id__in=ids).values_list("domain", flat=True),
            raise_on_error=raise_on_error,
        )

    def forget_schemas(self, schemas, *, raise_on_error=False):
        # Identify tenants by schema_name (the real identifier) — NOT by the human
        # company_name, which is a display label only.
        from tenants.models import Domain
        return self.forget_hosts(
            Domain.objects.filter(tenant__schema_name__in=schemas).values_list("domain", flat=True),
            raise_on_error=raise_on_error,
        )

    def forget_all(self, *, raise_on_error=False):
        """Delete every tenant_resolve snapshot / negative / tombstone via prefix-scoped
        delete_pattern over `<prefix>:<version>:host-snap:*` (NOT flushdb) — the host-snapshot
        sub-namespace only. The gate's structural keys live under the DISTINCT `treg:*`
        prefix, and any future service key under another logical prefix is outside `host-snap:*`,
        so they are NOT in this namespace and SURVIVE — including any in-flight reconcile lock.

        ⚠ Under the GATE stage this alone is DANGEROUS: `treg:hosts` survives, so every
        subsequent miss is an UNCAPPED member cold-fill → a thundering herd on `default`
        (the very thing the gate prevents). For a mass refresh under GATE use
        host_registry.run_locked() (reconcile: force-overwrite in place + atomic RENAME —
        no herd, no gap). The invalidate_resolve_cache --all command already routes to
        reconcile when WARM is on. See deploy/resolve_gate_design.md."""
        self._ensure_alive(raise_on_error)
        # scoped to the host-snapshot sub-namespace `<prefix>:<version>:host-snap:*` — NOT flushdb
        return self.cache.delete_pattern(f"{self._SNAP_PREFIX}*") or 0

    # ---- snapshot-namespace introspection (owns the django_redis key layout) ----
    def _snapshot_key_prefix(self):
        """Physical prefix django_redis puts on every cache key: '<KEY_PREFIX>:<version>:'.
        Owning it HERE keeps raw-SCAN callers (registry, bench) from hard-coding
        django_redis internals — if make_key/VERSION change, only this method changes."""
        return f"{self.cache.key_prefix}:{self.cache.version}:"

    def iter_snapshot_hosts(self):
        """Yield the logical hostname of every snapshot key (positive/negative/tombstone)
        currently in the cache, via SCAN of the `host-snap:` sub-namespace ONLY (service keys under
        other prefixes are excluded). Deleting during iteration is safe (SCAN cursor)."""
        c = self.get_redis_raw_client()
        physical = self._snapshot_key_prefix() + self._SNAP_PREFIX   # e.g. tres:1:host-snap:
        for raw in c.scan_iter(match=f"{physical}*", count=1000):
            key = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            yield key[len(physical):]

    _SWEEP_BATCH = 500

    def sweep_orphans(self, valid_hosts):
        """Delete orphan POSITIVE snapshots — hosts cached as a positive snapshot but no
        longer in `valid_hosts` (the DB). A positive HIT bypasses the gate/SET, so a
        lingering (possibly no-TTL) orphan would be served forever. Negatives/tombstones
        are left to self-expire (they don't serve a tenant). Returns the count deleted.

        Reads candidate values in batches (get_many) instead of one GET per key, so a sweep
        over a large key space is O(N / batch) round-trips, not O(N)."""
        swept, batch = 0, []
        for host in self.iter_snapshot_hosts():
            if host in valid_hosts:
                continue
            batch.append(host)
            if len(batch) >= self._SWEEP_BATCH:
                swept += self._sweep_batch(batch)
                batch = []
        return swept + self._sweep_batch(batch)

    def _sweep_batch(self, hosts):
        if not hosts:
            return 0
        found = self.cache.get_many([self._snap_key(h) for h in hosts])   # 1 RT; present only
        orphans = [k for k, v in found.items()                            # positives only
                   if self._classify(v) is self._Kind.POSITIVE]
        if orphans:
            self.cache.delete_many(orphans)                              # 1 RT
        return len(orphans)

    # ---- warm-up ----
    def warm(self, *, force=False, chunk=500, raise_on_error=False):
        """LEGACY positive-cache warm for the GATE-OFF path (flat _pos_ttl, no host SET).
        force=False: fill only ABSENT entries (nx, idempotent, respects tombstones).
        force=True: hard reload — overwrite everything with fresh DB data (batched).

        ⚠ Under the GATE/WARM stage do NOT use this — it writes flat-TTL snapshots and
        does not build/maintain `tres:hosts`. Use host_registry.run_locked() (reconcile:
        ttl_by_status + SET + orphan-sweep). The warm_resolve_cache command/task route to
        reconcile automatically when TENANT_REGISTRY_WARM_ENABLED is on."""
        from tenants.models import Domain
        pos_ttl = self._pos_ttl
        if not pos_ttl:
            return 0
        self._ensure_alive(raise_on_error)
        cache = self.cache
        qs = Domain.objects.select_related("tenant__shard").iterator(chunk_size=chunk)
        n = 0
        if force:
            batch = {}
            for d in qs:
                batch[self._snap_key(d.domain)] = self.dump(d.tenant)
                if len(batch) >= chunk:
                    cache.set_many(batch, pos_ttl)
                    n += len(batch)
                    batch = {}
            if batch:
                cache.set_many(batch, pos_ttl)
                n += len(batch)
        else:
            for d in qs:
                if cache.set(self._snap_key(d.domain), self.dump(d.tenant), pos_ttl, nx=True):
                    n += 1
        return n


resolve_cache = TenantResolveCache()   # module-level singleton