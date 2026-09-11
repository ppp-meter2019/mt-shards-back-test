"""Tenant-resolution cache — a single service object owning all CACHES["tenant_resolve"]
mechanics: markers, TTLs, dump/load, the nx+tombstone coherency protocol, invalidation
(one host / hosts / tenant / tenants / ids / names / all) and warm-up.

Read/write primitives (get_snapshot / put / put_many / store_miss / sweep_orphans) are
driven by the resolver service facade (tenants.resolver.service), NOT the middleware.
Fail-open (degrade to a DB resolve on any cache error) is the service's concern;
IGNORE_EXCEPTIONS masks Redis-down as a miss. redis_alive() bypasses that mask for
management commands and raise_on_error callers.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Container, Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

from django.core.cache import caches
from django.core.cache.backends.base import BaseCache
from django.db import transaction

from . import flags
from .config import resolve_cfg
from .markers import NEGATIVE, TOMBSTONE

if TYPE_CHECKING:                       # annotation-only: both are imported lazily inside the
    from tenants.models import Tenant   # methods that need them, and that stays that way
    from .snapshot import TenantSnapshot

logger = logging.getLogger(__name__)


class CacheUnavailable(RuntimeError):
    """Raised when raise_on_error=True and the tenant_resolve Redis is unreachable."""


class LegacyWarmRefused(RuntimeError):
    """Raised when warm() is called while the registry WARM stage is on — see the guard
    there. Subclasses RuntimeError because this is API MISUSE (a programming error), not an
    environment condition: unlike CacheUnavailable, nobody is expected to catch it."""


class TenantResolveCache:
    # get_snapshot() result sentinels (distinct from the string markers in the cache)
    MISS = object()
    NEG = object()
    HOLD = object()   # get_schema_snapshot only: an active invalidation tombstone (worker → DB)

    # Logical sub-namespace for per-host snapshot keys: physical key is
    # `<KEY_PREFIX>:<version>:host-snap:<host>` (e.g. tres:1:host-snap:<host>). Keeping
    # snapshots under a dedicated prefix means the SCAN/sweep operate ONLY on host snapshots
    # — any future auxiliary/service key stored via this Django cache (a different logical
    # prefix) or via the raw client (the gate's treg:* keys) is structurally excluded, so it
    # can never be mistaken for a host and swept. All host↔key mapping goes through _snap_key().
    _SNAP_PREFIX = "host-snap:"
    # Parallel sub-namespace for SCHEMA-keyed snapshots (`schema-snap:<schema>`), the stable
    # per-tenant routing key used by the worker path. Same VALUE as a host snapshot (dump/load);
    # only the KEY differs. Kept in lockstep with host-snap by every tenant-scoped mutation
    # (see _keys_for + the forget_* family). Structurally distinct from the gate's `treg:*`.
    _SCHEMA_PREFIX = "schema-snap:"

    def __init__(self, cache: BaseCache | None = None) -> None:
        self._cache = cache            # DI for tests; else resolved lazily

    @property
    def cache(self) -> BaseCache:
        # DI (tests) wins; otherwise resolve per access. caches[...] is a cheap
        # thread-local lookup Django already caches per thread AND recycles on
        # request_finished — so we neither share one instance across threads nor
        # hold a closed connection across requests.
        return self._cache if self._cache is not None else caches["tenant_resolve"]

    def get_redis_raw_client(self, write: bool = True) -> Any:
        """The underlying redis-py client for CACHES['tenant_resolve'], bypassing the
        django-cache wrapper. Use it for ops the Django cache API doesn't expose —
        SET NX/EX, INCR, EXPIRE, SISMEMBER, SCAN, pipeline, ping. Unlike the wrapped
        cache, errors here PROPAGATE (no IGNORE_EXCEPTIONS masking), so callers must
        handle them explicitly (fail-open). Single source for all raw-client access."""
        return self.cache.client.get_client(write=write)

    # ---- config (read live so override_settings works) ----
    @property
    def _pos_ttl(self) -> int:
        return resolve_cfg.POSITIVE_CACHE_SECONDS

    def ttl_for_status(self, status: str) -> int | None:
        """Positive-snapshot TTL by tenant status when the registry WARM stage is on.
        Returns None for statuses mapped to no-expiry (e.g. ACTIVE); falls back to the
        flat _pos_ttl for statuses not listed. Used by store() and the reconcile task."""
        mapping = resolve_cfg.WARM_TTL_BY_STATUS
        return mapping[status] if status in mapping else self._pos_ttl

    @property
    def _neg_ttl(self) -> int:
        return resolve_cfg.MISS_CACHE_SECONDS

    @property
    def _hold(self) -> int:
        return resolve_cfg.HOLD_SECONDS

    @property
    def warm_enabled(self) -> bool:
        return flags.warm_enabled()

    @property
    def enabled(self) -> bool:
        # WARM counts too: under WARM positives are written with ttl_by_status, so the flat
        # _pos_ttl can be 0 while the cache is fully in use. Without WARM here, the resolve
        # short-circuit in service.resolve() would skip a cache the reconcile keeps filled.
        return bool(self.warm_enabled or self._pos_ttl or self._neg_ttl)

    # ---- health (bypasses IGNORE_EXCEPTIONS) ----
    def redis_alive(self) -> bool:
        try:
            return bool(self.get_redis_raw_client().ping())
        except Exception:
            return False

    def _ensure_alive(self, raise_on_error: bool) -> None:
        if raise_on_error and not self.redis_alive():
            raise CacheUnavailable("tenant_resolve Redis is not reachable")

    # ---- serialization ----
    # Explicit allowlist of the fields a snapshot carries. Deliberately NOT derived from
    # TenantSnapshot's dataclass fields: `domain_url` is one of those, but it is PER-REQUEST
    # (django_tenants writes the incoming Host onto it), so caching it would stamp one
    # hostname onto a snapshot shared by ALL of a tenant's domains.
    # Adding a field here puts that value into Redis (tenant_resolve, db2), so it is a
    # deliberate act — which is why company_name / description / last_error / timestamps
    # stay OUT. There is no longer a "never read anything else" rule to remember: load()
    # returns a TenantSnapshot, which simply has no other fields.
    # No `shard_id`: it was the model's FK attname, duplicating shard.id in every payload,
    # and it existed only to keep a rebuilt model instance self-consistent.
    _SNAPSHOT_FIELDS = {
        "tenant": ("id", "schema_name", "status"),
        "shard":  ("id", "alias"),
    }

    @classmethod
    def dump(cls, tenant: Tenant | TenantSnapshot) -> dict[str, Any]:
        """Serialize a tenant's routing snapshot for the cache.

        Accepts a Tenant OR a TenantSnapshot: the resolve path hands over an already
        captured snapshot, while reconcile and warm hand over rows straight off the Domain
        queryset. capture() is idempotent, so normalizing first keeps ONE type inside this
        method and stops the payload keys drifting from the dataclass.
        """
        from .snapshot import TenantSnapshot
        snap = TenantSnapshot.capture(tenant)
        return {
            "tenant": {f: getattr(snap, f) for f in cls._SNAPSHOT_FIELDS["tenant"]},
            "shard":  {f: getattr(snap.shard, f) for f in cls._SNAPSHOT_FIELDS["shard"]},
        }

    @classmethod
    def load(cls, data: dict[str, Any]) -> TenantSnapshot:
        """Rebuild the routing snapshot from a cached payload.

        Reads only the declared keys, so a payload written by an older release (which also
        carried `shard_id`) loads unchanged — and an older release reading a payload written
        here tolerates its absence too, which is what makes a rolling deploy safe without
        bumping the cache version.
        """
        from .snapshot import ShardSnapshot, TenantSnapshot
        t, s = data["tenant"], data["shard"]
        return TenantSnapshot(
            id=t["id"], schema_name=t["schema_name"], status=t["status"],
            shard=ShardSnapshot(id=s["id"], alias=s["alias"]),
        )

    # ---- key namespacing ----
    def _snap_key(self, hostname: str) -> str:
        """Logical cache key for a host snapshot (host → 'host-snap:<host>'). The single place that
        maps a hostname to its cache key; callers pass/receive bare hostnames."""
        return f"{self._SNAP_PREFIX}{hostname}"

    def _schema_snap_key(self, schema: str) -> str:
        """Logical cache key for a SCHEMA snapshot (schema → 'schema-snap:<schema>')."""
        return f"{self._SCHEMA_PREFIX}{schema}"

    def _keys_for(self, tenant: Tenant) -> list[str]:
        """ALL snapshot keys a tenant owns: one host-snap per domain + its single schema-snap.
        The single source of truth for a tenant's cache identity — every tenant-scoped mutation
        flows through this so the two namespaces cannot drift."""
        keys = [self._snap_key(h) for h in tenant.domains.values_list("domain", flat=True)]
        keys.append(self._schema_snap_key(tenant.schema_name))
        return keys

    # ---- value classification (single source: what a raw cached value is) ----
    class _Kind:
        POSITIVE = "positive"   # a real snapshot payload (dict)
        NEG      = "neg"        # cached-miss marker
        HOLD     = "hold"       # tombstone (invalidation hold)
        MISS     = "miss"       # absent OR Redis-down (None)
        UNKNOWN  = "unknown"    # corrupt / unexpected type

    @classmethod
    def _classify(cls, cached: Any) -> str:
        """Categorize a raw cached value. The ONE place that knows the on-wire shapes —
        get_snapshot, sweep and the bench all route through this."""
        if cached is None:            return cls._Kind.MISS
        if cached == TOMBSTONE:       return cls._Kind.HOLD
        if cached == NEGATIVE:        return cls._Kind.NEG
        if isinstance(cached, dict) and "tenant" in cached and "shard" in cached:
            return cls._Kind.POSITIVE          # has the shape load() understands
        return cls._Kind.UNKNOWN               # non-dict OR malformed dict → treated as a miss

    # ---- read primitives (used by middleware) ----
    def get_snapshot(self, hostname: str) -> TenantSnapshot | object:
        """MISS (absent/tombstone/Redis-down/corrupt) | NEG (cached miss) | reconstructed Tenant."""
        cached = self.cache.get(self._snap_key(hostname))  # None on absent OR Redis error
        kind = self._classify(cached)
        if kind is self._Kind.POSITIVE:
            return self.load(cached)
        if kind is self._Kind.NEG:
            return self.NEG
        return self.MISS                          # MISS / HOLD / UNKNOWN → treat as a miss

    def get_schema_snapshot(self, schema: str) -> TenantSnapshot | object:
        """Worker-path read (schema-keyed). POSITIVE reconstructed Tenant | HOLD (an active
        invalidation tombstone) | MISS (absent OR Redis-down). HOLD is kept DISTINCT from MISS
        so the worker bypasses its stale local cache on a fresh invalidation and re-resolves
        from the DB. No NEG: schema-snap is positive-only (a trusted internal schema is never
        negative-cached)."""
        cached = self.cache.get(self._schema_snap_key(schema))
        kind = self._classify(cached)
        if kind is self._Kind.POSITIVE:
            return self.load(cached)
        if kind is self._Kind.HOLD:
            return self.HOLD
        return self.MISS                          # absent / Redis-down / (corrupt) → miss

    def _ttl_for(self, tenant: Tenant | TenantSnapshot, warm: bool) -> int | None:
        """Positive-snapshot TTL for a tenant: ttl_by_status under WARM, else flat _pos_ttl."""
        return self.ttl_for_status(tenant.status) if warm else self._pos_ttl

    def put(self, hostname: str, tenant: Tenant | TenantSnapshot) -> bool:
        """Resolve-path fill (nx) — the FRONT's warmer. Writes BOTH the host-snap AND the
        tenant's schema-snap (lockstep with the forget_* invalidation), so any front resolve
        also warms the worker's schema-keyed cache — including re-warming a just-recovered
        cache. The WORKER never writes here (it is a pure consumer); the shared cache's only
        writers are this resolve path and reconcile (put_many / put_schema_many).

        nx: a hold (tombstone) is never overwritten and a slow resolver can't resurrect stale
        data. TTL: ttl_by_status under WARM (ACTIVE→None=no expiry) else flat _pos_ttl; a no-op
        when positive caching is off (legacy _pos_ttl=0, WARM off).

        Returns whether the HOST snapshot was actually written — False when positive caching
        is off, or when nx declined (a tombstone is held, or another writer got there first).
        The return value is the OUTCOME, not "caching is enabled": under nx the write can
        legitimately not land, and a caller branching on it must see that. The schema-snap
        result is deliberately not folded in — the host key is what the caller asked to fill."""
        warm = self.warm_enabled
        if not warm and not self._pos_ttl:
            return False
        ttl = self._ttl_for(tenant, warm)
        payload = self.dump(tenant)
        filled = bool(self.cache.set(self._snap_key(hostname), payload, ttl, nx=True))
        self.cache.set(self._schema_snap_key(tenant.schema_name), payload, ttl, nx=True)
        return filled

    def _put_many_by_ttl(self, keyed_tenants: Iterable[tuple[str, Tenant | TenantSnapshot]]) -> int:
        """Batched FORCE-write of positive snapshots, grouped by TTL — the shared core of
        put_many() / put_schema_many(). `keyed_tenants` yields (cache_key, tenant); the CALLER
        owns the host↔key vs schema↔key mapping, mirroring how _sweep_namespace takes a key_fn
        on the DELETE side. One set_many per distinct TTL (django_redis pipelines each).
        Returns the number written; a no-op when positive caching is off.

        The two public wrappers stay separate on purpose: they serve DIFFERENT namespaces with
        different readers and cardinality (N host-snaps vs 1 schema-snap per tenant). Only the
        grouping loop is common, and it lives here so a future change (chunking, pipelining, a
        metrics counter) cannot land in one copy and miss the other."""
        warm = self.warm_enabled
        if not warm and not self._pos_ttl:
            return 0
        by_ttl = {}
        for key, tenant in keyed_tenants:
            by_ttl.setdefault(self._ttl_for(tenant, warm), {})[key] = self.dump(tenant)
        n = 0
        for ttl, batch in by_ttl.items():
            self.cache.set_many(batch, ttl)
            n += len(batch)
        return n

    def put_many(self, items: Iterable[tuple[str, Tenant | TenantSnapshot]]) -> int:
        """Batched FORCE-write of HOST snapshots — the reconcile path. `items` is an iterable
        of (hostname, tenant); one entry per Domain row. Returns the number written.

        FORCE semantics: overwrites unconditionally — the 5s hold (tombstone) is NOT
        respected here. That is deliberate: reconcile is the authoritative single writer
        that just read the DB, so writing fresh data over a hold is correct; the hold only
        guards the resolve path's nx race (put/store), and the real mid-rebuild races are
        handled by the dirty-recheck + sweep_orphans, not by the hold."""
        return self._put_many_by_ttl((self._snap_key(h), t) for h, t in items)

    def put_schema_many(self, tenants: Iterable[Tenant | TenantSnapshot]) -> int:
        """Batched FORCE-write of SCHEMA snapshots — the reconcile path's schema-keyed sibling
        of put_many(), read by the Celery worker (TenantTask.get_tenant_for_schema). `tenants`
        is an iterable of Tenant, deduped by schema upstream (a tenant has N domains but ONE
        schema). Same FORCE semantics as put_many(); returns the count written."""
        return self._put_many_by_ttl(
            (self._schema_snap_key(t.schema_name), t) for t in tenants)

    def store_miss(self, hostname: str) -> None:
        if self._neg_ttl:
            self.cache.set(self._snap_key(hostname), NEGATIVE, self._neg_ttl, nx=True)

    # ---- invalidation (host-snap AND schema-snap kept in lockstep) ----
    def _tombstone_keys(self, keys: Iterable[str]) -> int:
        """The ONE low-level invalidation primitive: deferred (on_commit) TOMBSTONE — or
        delete when hold is disabled — of an explicit key list. `keys` may freely mix host-snap
        and schema-snap keys, so both namespaces drop together. Returns the key count."""
        keys = [k for k in keys if k]
        if not keys:
            return 0
        cache, hold = self.cache, self._hold

        def _invalidate() -> None:
            if hold:
                cache.set_many({k: TOMBSTONE for k in keys}, hold)   # hold marker
            else:
                cache.delete_many(keys)                              # hold disabled

        transaction.on_commit(_invalidate)
        return len(keys)

    def forget_hosts(self, hostnames: Iterable[str], *, schemas: Iterable[str] | None = None,
                     raise_on_error: bool = False) -> int:
        """Invalidate hosts AND the schema-snap of their tenants. `schemas` are dropped
        explicitly (reliable — the caller knew the tenant). For bare-host callers that pass no
        schema (Domain signals, the tenant-delete cascade) the schema is derived BEST-EFFORT
        from each host's cached snapshot; a cold/absent host snapshot leaves its schema-snap to
        the Tenant post_delete receiver (delete) or the reconcile orphan-sweep."""
        self._ensure_alive(raise_on_error)   # checked first — honored even for an empty target
        hostnames = [h for h in hostnames if h]
        schemas = {s for s in (schemas or ()) if s}
        host_keys = [self._snap_key(h) for h in hostnames]
        if hostnames:                                            # best-effort schema derive
            for v in self.cache.get_many(host_keys).values():
                if self._classify(v) is self._Kind.POSITIVE:
                    schemas.add(v["tenant"]["schema_name"])
        return self._tombstone_keys(host_keys + [self._schema_snap_key(s) for s in schemas])

    def forget_host(self, hostname: str, *, raise_on_error: bool = False) -> int:
        return self.forget_hosts([hostname], raise_on_error=raise_on_error)

    def forget_tenant(self, tenant: Tenant, *, raise_on_error: bool = False) -> int:
        # Tenant known → drop its FULL key set (_keys_for): every domain's host-snap + its
        # schema-snap. Schema is reliable here — no cache-derive needed.
        self._ensure_alive(raise_on_error)
        return self._tombstone_keys(self._keys_for(tenant))

    def forget_tenants(self, tenants: Iterable[Tenant], *, raise_on_error: bool = False) -> int:
        from tenants.models import Domain
        tenants = list(tenants)
        schemas = {t.schema_name for t in tenants}
        hosts = Domain.objects.filter(tenant__in=tenants).values_list("domain", flat=True)
        return self.forget_hosts(hosts, schemas=schemas, raise_on_error=raise_on_error)

    def forget_ids(self, ids: Iterable[int], *, raise_on_error: bool = False) -> int:
        # Schema from Tenant (covers a tenant with ZERO domains); hosts from Domain.
        from tenants.models import Domain, Tenant
        schemas = set(Tenant.objects.filter(id__in=ids).values_list("schema_name", flat=True))
        hosts = Domain.objects.filter(tenant_id__in=ids).values_list("domain", flat=True)
        return self.forget_hosts(hosts, schemas=schemas, raise_on_error=raise_on_error)

    def forget_schemas(self, schemas: Iterable[str], *, raise_on_error: bool = False) -> int:
        # Identify tenants by schema_name (the real identifier) — NOT by the human
        # company_name, which is a display label only. schemas are KNOWN → the schema-snap drop
        # is reliable; hosts are looked up to also drop their host-snaps.
        from tenants.models import Domain
        schemas = set(schemas)
        hosts = Domain.objects.filter(tenant__schema_name__in=schemas).values_list("domain", flat=True)
        return self.forget_hosts(hosts, schemas=schemas, raise_on_error=raise_on_error)

    def forget_all(self, *, raise_on_error: bool = False) -> int:
        """Delete every snapshot — BOTH `host-snap:*` and `schema-snap:*` — via prefix-scoped
        delete_pattern (NOT flushdb). The gate's structural keys live under the DISTINCT `treg:*`
        prefix, and any future service key under another logical prefix is outside both snapshot
        sub-namespaces, so they SURVIVE — including any in-flight reconcile lock.

        ⚠ Under the GATE stage this alone is DANGEROUS: `treg:hosts` survives, so every
        subsequent miss is an UNCAPPED member cold-fill → a thundering herd on `default`
        (the very thing the gate prevents). For a mass refresh under GATE use
        host_registry.run_locked() (reconcile: force-overwrite in place + atomic RENAME —
        no herd, no gap). The invalidate_resolve_cache --all command already routes to
        reconcile when WARM is on. See deploy/resolve_gate_design.md."""
        self._ensure_alive(raise_on_error)
        n = self.cache.delete_pattern(f"{self._SNAP_PREFIX}*") or 0
        n += self.cache.delete_pattern(f"{self._SCHEMA_PREFIX}*") or 0
        return n

    # ---- snapshot-namespace introspection (owns the django_redis key layout) ----
    def _snapshot_key_prefix(self) -> str:
        """Physical prefix django_redis puts on every cache key: '<KEY_PREFIX>:<version>:'.
        Owning it HERE keeps raw-SCAN callers (registry, bench) from hard-coding
        django_redis internals — if make_key/VERSION change, only this method changes."""
        return f"{self.cache.key_prefix}:{self.cache.version}:"

    def iter_snapshot_hosts(self) -> Iterator[str]:
        """Yield the logical hostname of every snapshot key (positive/negative/tombstone)
        currently in the cache, via SCAN of the `host-snap:` sub-namespace ONLY (service keys under
        other prefixes are excluded). Deleting during iteration is safe (SCAN cursor)."""
        c = self.get_redis_raw_client()
        physical = self._snapshot_key_prefix() + self._SNAP_PREFIX   # e.g. tres:1:host-snap:
        for raw in c.scan_iter(match=f"{physical}*", count=1000):
            key = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            yield key[len(physical):]

    def iter_snapshot_schemas(self) -> Iterator[str]:
        """Yield the logical schema_name of every schema-snap key currently in the cache, via
        SCAN of the `schema-snap:` sub-namespace ONLY. Sibling of iter_snapshot_hosts()."""
        c = self.get_redis_raw_client()
        physical = self._snapshot_key_prefix() + self._SCHEMA_PREFIX   # e.g. tres:1:schema-snap:
        for raw in c.scan_iter(match=f"{physical}*", count=1000):
            key = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            yield key[len(physical):]

    _SWEEP_BATCH = 500

    def sweep_orphans(self, valid_hosts: Container[str],
                      valid_schemas: Container[str] = frozenset()) -> int:
        """Delete orphan POSITIVE snapshots in BOTH namespaces: a host-snap whose host is no
        longer in `valid_hosts`, and a schema-snap whose schema is not in `valid_schemas` (the
        DB truth). A positive HIT bypasses the gate/SET, so a lingering (possibly no-TTL) orphan
        would be served forever. Negatives/tombstones self-expire (they serve no tenant).
        Returns the total deleted. Batched (get_many/delete_many) — O(N / batch) round-trips.

        NB: `valid_schemas` must be ALL tenant schemas (incl. domainless tenants), NOT only
        schemas-with-domains — otherwise a valid domainless tenant's lazily-filled schema-snap
        would be swept every reconcile."""
        return (self._sweep_namespace(self.iter_snapshot_hosts(), valid_hosts, self._snap_key)
                + self._sweep_namespace(self.iter_snapshot_schemas(), valid_schemas, self._schema_snap_key))

    def _sweep_namespace(self, logical_iter: Iterable[str], valid: Container[str],
                         key_fn: Callable[[str], str]) -> int:
        swept, batch = 0, []
        for name in logical_iter:
            if name in valid:
                continue
            batch.append(name)
            if len(batch) >= self._SWEEP_BATCH:
                swept += self._sweep_batch(batch, key_fn)
                batch = []
        return swept + self._sweep_batch(batch, key_fn)

    def _sweep_batch(self, names: Sequence[str], key_fn: Callable[[str], str]) -> int:
        if not names:
            return 0
        found = self.cache.get_many([key_fn(n) for n in names])   # 1 RT; present only
        orphans = [k for k, v in found.items()                    # positives only
                   if self._classify(v) is self._Kind.POSITIVE]
        if orphans:
            self.cache.delete_many(orphans)                       # 1 RT
        return len(orphans)

    # ---- warm-up ----
    def warm(self, *, force: bool = False, chunk: int = 500,
             raise_on_error: bool = False) -> int:
        """Positive-cache warm for the GATE-OFF path (flat _pos_ttl, no host SET).
        force=False: fill only ABSENT entries (nx, idempotent, respects tombstones).
        force=True: hard reload — overwrite everything with fresh DB data (batched).

        Writes BOTH namespaces, like put() and reconcile: a host-snap per Domain and a
        schema-snap per DISTINCT tenant. Both are required for a warm to mean anything: the
        WORKER reads only the schema-keyed side (TenantTask.get_tenant_for_schema ->
        get_schema_snapshot), so warming hosts alone leaves every task resolving from the DB
        until some front request happens to fill the schema-snap through put().

        REFUSES to run under the GATE/WARM stage (LegacyWarmRefused) — see the guard below.
        Use host_registry.run_locked() (reconcile: ttl_by_status + SET + orphan-sweep) there;
        the warm_resolve_cache command/task already route to it when WARM is on."""
        # ENFORCED, not merely documented. Under WARM this method is the wrong tool and its
        # damage is SILENT: it writes positives without building/maintaining `treg:hosts`, so
        # it refreshes exactly what the SET is supposed to gate while leaving the SET stale —
        # and a forget_all-then-warm sequence becomes an uncapped member cold-fill herd on
        # `default`, the very thing the gate exists to prevent.
        #
        # Placed FIRST, before the _pos_ttl early-return: _pos_ttl may legitimately be 0 while
        # WARM is on (see the `enabled` property), and in that combination — the worst one —
        # the early return would swallow the misuse as a silent `return 0`.
        #
        # NOT gated by raise_on_error: that flag is about Redis being unreachable (an
        # environment condition). A wrong call is a bug and must always be loud.
        if self.warm_enabled:
            raise LegacyWarmRefused(
                "TenantResolveCache.warm() is not usable while TENANT_REGISTRY"
                "['WARM_ENABLED'] is on: it writes flat-TTL positives and does NOT build or "
                "maintain the `treg:hosts` SET the gate reads. Use host_registry.run_locked() "
                "(reconcile), or the commands that already route to it: "
                "`manage.py warm_resolve_cache` / `manage.py invalidate_resolve_cache --all`."
            )
        from tenants.models import Domain
        if not self._pos_ttl:
            return 0
        self._ensure_alive(raise_on_error)
        # CHUNKED at THIS level on purpose: put_many / _put_many_by_ttl materialize every
        # payload they are handed, so bounding memory is the CALLER's job — the same contract
        # HostRegistry._rebuild_once relies on. Handing them the whole queryset would build one
        # dict of every domain in the platform and issue a single enormous set_many.
        rows = Domain.objects.select_related("tenant__shard").iterator(chunk_size=chunk)
        n = 0
        for batch in self._iter_chunks(rows, chunk):
            # One schema-snap per DISTINCT tenant: a tenant has N domains but ONE schema.
            by_schema = {d.tenant.schema_name: d.tenant for d in batch}
            if force:
                n += self.put_many((d.domain, d.tenant) for d in batch)
                self.put_schema_many(by_schema.values())
            else:
                n += self._fill_absent(batch, by_schema)
        return n

    @staticmethod
    def _iter_chunks(iterable: Iterable[Any], size: int) -> Iterator[list[Any]]:
        """Yield lists of at most `size` items — the memory bound put_many callers must
        provide (see the note in warm())."""
        batch = []
        for item in iterable:
            batch.append(item)
            if len(batch) >= size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _fill_absent(self, rows: Iterable[Any], by_schema: dict[str, Tenant]) -> int:
        """nx half of warm(): fill only ABSENT entries in BOTH namespaces, so a tombstone is
        never overwritten. Per-key SET NX because no batch API carries nx (set_many cannot),
        which is why the schema side is deduped by the caller — otherwise a tenant with N
        domains would cost N redundant schema-snap writes instead of one.

        Returns the number of HOST snapshots actually filled; schema-snaps are not counted,
        so the total still means "domains warmed"."""
        ttl, cache = self._pos_ttl, self.cache
        filled = 0
        for d in rows:
            if cache.set(self._snap_key(d.domain), self.dump(d.tenant), ttl, nx=True):
                filled += 1
        for schema, tenant in by_schema.items():
            cache.set(self._schema_snap_key(schema), self.dump(tenant), ttl, nx=True)
        return filled


resolve_cache = TenantResolveCache()   # module-level singleton