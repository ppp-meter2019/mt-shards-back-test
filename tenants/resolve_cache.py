"""Tenant-resolution cache — a single service object owning all CACHES["tenant_resolve"]
mechanics: markers, TTLs, dump/load, the nx+tombstone coherency protocol, invalidation
(one host / hosts / tenant / tenants / ids / names / all) and warm-up.

Read primitives (get_snapshot/store/store_miss) are used by ShardAwareTenantMiddleware.
Fail-open (degrade to a DB resolve on any cache error) is the middleware's concern;
IGNORE_EXCEPTIONS masks Redis-down as a miss. redis_alive() bypasses that mask for
management commands and raise_on_error callers.
"""
import logging

from django.conf import settings
from django.core.cache import caches
from django.db import transaction

from .resolve_markers import NEGATIVE, TOMBSTONE

logger = logging.getLogger(__name__)


class CacheUnavailable(RuntimeError):
    """Raised when raise_on_error=True and the tenant_resolve Redis is unreachable."""


class TenantResolveCache:
    # get_snapshot() result sentinels (distinct from the string markers in the cache)
    MISS = object()
    NEG = object()

    def __init__(self, cache=None):
        self._cache = cache            # DI for tests; else resolved lazily

    @property
    def cache(self):
        # DI (tests) wins; otherwise resolve per access. caches[...] is a cheap
        # thread-local lookup Django already caches per thread AND recycles on
        # request_finished — so we neither share one instance across threads nor
        # hold a closed connection across requests.
        return self._cache if self._cache is not None else caches["tenant_resolve"]

    # ---- config (read live so override_settings works) ----
    @property
    def _pos_ttl(self):
        return getattr(settings, "TENANT_RESOLVE_CACHE_SECONDS", 0)

    @property
    def _neg_ttl(self):
        return getattr(settings, "TENANT_RESOLVE_MISS_CACHE_SECONDS", 0)

    @property
    def _hold(self):
        return getattr(settings, "TENANT_RESOLVE_HOLD_SECONDS", 5)

    @property
    def enabled(self):
        return bool(self._pos_ttl or self._neg_ttl)

    # ---- health (bypasses IGNORE_EXCEPTIONS) ----
    def redis_alive(self):
        try:
            return bool(self.cache.client.get_client(write=True).ping())
        except Exception:
            return False

    def _ensure_alive(self, raise_on_error):
        if raise_on_error and not self.redis_alive():
            raise CacheUnavailable("tenant_resolve Redis is not reachable")

    # ---- serialization ----
    @staticmethod
    def dump(tenant):
        shard = tenant.shard
        return {
            "tenant": {f.attname: getattr(tenant, f.attname) for f in tenant._meta.fields},
            "shard":  {f.attname: getattr(shard,  f.attname) for f in shard._meta.fields},
        }

    @staticmethod
    def _build(model, values):
        fields = {f.attname for f in model._meta.fields}
        return model(**{k: v for k, v in values.items() if k in fields})

    @classmethod
    def load(cls, data):
        from .models import Shard, Tenant
        tenant = cls._build(Tenant, data["tenant"])
        shard = cls._build(Shard, data["shard"])
        shard.read_only = True
        tenant.shard = shard                 # real pk -> shard_id consistent; .alias needs no DB
        tenant.read_only = True
        return tenant

    # ---- read primitives (used by middleware) ----
    def get_snapshot(self, hostname):
        """MISS (absent/tombstone/Redis-down) | NEG (cached miss) | reconstructed Tenant."""
        cached = self.cache.get(hostname)        # None on absent OR Redis error (fail-open)
        if cached is None or cached == TOMBSTONE:
            return self.MISS
        if cached == NEGATIVE:
            return self.NEG
        return self.load(cached)                 # may raise on corrupt -> middleware falls back

    def store(self, hostname, tenant):
        if self._pos_ttl:
            self.cache.set(hostname, self.dump(tenant), self._pos_ttl, nx=True)  # nx: keep hold marker

    def store_miss(self, hostname):
        if self._neg_ttl:
            self.cache.set(hostname, NEGATIVE, self._neg_ttl, nx=True)

    # ---- invalidation ----
    def forget_hosts(self, hostnames, *, raise_on_error=False):
        self._ensure_alive(raise_on_error)   # checked first — honored even for an empty target
        hostnames = [h for h in hostnames if h]
        if not hostnames:
            return 0
        cache, hold = self.cache, self._hold

        def _invalidate():
            if hold:
                cache.set_many({h: TOMBSTONE for h in hostnames}, hold)   # hold marker
            else:
                cache.delete_many(hostnames)                              # hold disabled

        transaction.on_commit(_invalidate)
        return len(hostnames)

    def forget_host(self, hostname, *, raise_on_error=False):
        return self.forget_hosts([hostname], raise_on_error=raise_on_error)

    def forget_tenant(self, tenant, *, raise_on_error=False):
        return self.forget_hosts(tenant.domains.values_list("domain", flat=True),
                          raise_on_error=raise_on_error)

    def forget_tenants(self, tenants, *, raise_on_error=False):
        from .models import Domain
        return self.forget_hosts(
            Domain.objects.filter(tenant__in=tenants).values_list("domain", flat=True),
            raise_on_error=raise_on_error,
        )

    def forget_ids(self, ids, *, raise_on_error=False):
        from .models import Domain
        return self.forget_hosts(
            Domain.objects.filter(tenant_id__in=ids).values_list("domain", flat=True),
            raise_on_error=raise_on_error,
        )

    def forget_names(self, names, *, raise_on_error=False):
        from .models import Domain
        return self.forget_hosts(
            Domain.objects.filter(tenant__name__in=names).values_list("domain", flat=True),
            raise_on_error=raise_on_error,
        )

    def forget_all(self, *, raise_on_error=False):
        self._ensure_alive(raise_on_error)
        return self.cache.delete_pattern("*") or 0   # scoped to KEY_PREFIX 'tres' — NOT flushdb

    # ---- warm-up ----
    def warm(self, *, force=False, chunk=500, raise_on_error=False):
        """force=False: fill only ABSENT entries (nx, idempotent, respects tombstones).
        force=True: hard reload — overwrite everything with fresh DB data (batched)."""
        from .models import Domain
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
                batch[d.domain] = self.dump(d.tenant)
                if len(batch) >= chunk:
                    cache.set_many(batch, pos_ttl)
                    n += len(batch)
                    batch = {}
            if batch:
                cache.set_many(batch, pos_ttl)
                n += len(batch)
        else:
            for d in qs:
                if cache.set(d.domain, self.dump(d.tenant), pos_ttl, nx=True):
                    n += 1
        return n


resolve_cache = TenantResolveCache()   # module-level singleton