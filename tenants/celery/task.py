"""Shard+schema-aware TenantTask — multi-DB adaptation of
tenant_schemas_celery.task.

Carries only `_schema_name` in the message headers (like upstream); the shard
is resolved on the worker from Tenant.shard via get_tenant_for_schema. The
schema is read from the ACTIVE shard connection, not `default`. The actual
switch/restore happens per-invocation in TenantTask.__call__.
"""
import copy
from typing import Optional

from .cache import SimpleCache
from .compat import current_schema_name, get_public_schema_name, tenant_context, use_alias

# Celery >= 5.4 (pinned in requirements: celery[redis]>=5.4,<6) ships DjangoTask, which closes
# stale DB connections after each task. The pin guarantees it, so there is NO pre-5.4 fallback
# (the old `except ImportError: BaseTask = Task` promised close_old_connections signals it never
# wired — a misleading no-op; removed with the pin).
from celery.contrib.django.task import DjangoTask
BaseTask = DjangoTask

_shared_storage = {}


class SharedTenantCache(SimpleCache):
    def __init__(self):
        super().__init__(storage=_shared_storage)


def headers_with_schema(headers: Optional[dict]) -> dict:
    """Stamp the caller's schema (from the active shard) into headers if absent."""
    if headers and "_schema_name" in headers:
        return headers
    headers = copy.deepcopy(headers) if headers else {}
    headers["_schema_name"] = current_schema_name()
    return headers


def _schema_from_request(task):
    """Read _schema_name from the task message (headers, or merged request). Empty on a bare
    in-process call `task(...)` (no request was pushed) — TenantTask.__call__ handles that."""
    req = task.request
    if req.headers and "_schema_name" in req.headers:    # Redis broker merges headers
        return req.headers.get("_schema_name")
    return req.get("_schema_name")


class TenantTask(BaseTask):
    abstract = True
    tenant_cache_seconds = None

    def __call__(self, *args, **kwargs):
        """Enter the tenant's shard+schema for EXACTLY this invocation; the context manager's
        finally restores it whether the task returns OR raises, and regardless of worker pool.
        Replaces the old task_prerun/postrun pair (two signals with no shared finally + state
        stashed on the task singleton). Branches:
          * worker / apply_async / .delay / eager .apply()  -> headers carry _schema_name
            (CeleryApp.send_task and apply() always stamp it) -> switch to that schema;
          * public/management  -> pin the router axis to 'default' (don't trust ambient);
          * bare in-process call `task(...)` (no request)  -> inherit the CALLER's ambient
            context, exactly as before (the old signals never fired off the worker/eager path).
        """
        schema = _schema_from_request(self)
        if not schema:                                     # bare direct call: no message context
            return super().__call__(*args, **kwargs)
        if schema == get_public_schema_name():
            with use_alias("default"):                     # router axis clean; don't trust ambient
                return super().__call__(*args, **kwargs)
        with tenant_context(self.get_tenant_for_schema(schema)):
            return super().__call__(*args, **kwargs)

    @classmethod
    def tenant_cache(cls):
        return SharedTenantCache()

    @classmethod
    def get_tenant_for_schema(cls, schema_name):
        """Resolve schema -> Tenant(+shard), minimizing default-DB load. Order:
          1. GLOBAL shared cache (schema-snap — the INVALIDATED one) — authoritative when up,
             so all workers route on fresh data. A HOLD (active invalidation) skips straight to
             the DB (never trust the possibly-stale local cache during a fresh invalidation).
          2. LOCAL per-worker cache — a PURE outage/cold fallback.
          3. DB.
        The worker is a pure CONSUMER of the global cache — it never writes it (the front
        resolve path + reconcile are its warmers). And L1 is touched ONLY when the global cache
        does NOT serve the entry: while the global cache serves a POSITIVE hit, L1 is neither
        read nor written; a HOLD resolves fresh from the DB WITHOUT touching L1 (the invalidation
        is transient and the global cache is up); L1 is read+filled only on a global MISS
        (absent/cold OR down). So while the global cache is healthy, L1 stays empty and its
        entries self-expire (CELERY_TASK_TENANT_CACHE_SECONDS) — it fills only during a global
        outage/cold window and drains ~one TTL after recovery. When the global cache is disabled
        entirely, L1 is the sole cache (filled on every resolve). See
        deploy/celery_fanout_design.md (schema-snap) / resolve_gate_design.md."""
        from tenants.resolver import resolve_cache

        if resolve_cache.enabled:                              # global cache in use
            snap = resolve_cache.get_schema_snapshot(schema_name)
            if snap is resolve_cache.HOLD:                     # fresh invalidation → DB, L1 untouched
                return cls._db_get(schema_name)
            if snap is not resolve_cache.MISS:                 # POSITIVE hit → return, L1 untouched
                return snap
            # MISS = absent(cold) OR global down → L1 fallback below.

        L1 = cls.tenant_cache()
        missing = object()
        tenant = L1.get(schema_name, default=missing)
        if tenant is not missing:
            return tenant
        tenant = cls._db_get(schema_name)
        L1.set(schema_name, tenant, expire_seconds=cls._l1_seconds())   # fill L1 ONLY on fallback
        return tenant

    @classmethod
    def _l1_seconds(cls):
        s = cls.tenant_cache_seconds
        if s is None:
            s = int(getattr(cls.app.conf, "task_tenant_cache_seconds", 0) or 0)
        return s

    @classmethod
    def _db_get(cls, schema_name):
        """Authoritative DB resolve (no cache writes of any kind)."""
        from tenants.models import Tenant
        return Tenant.objects.select_related("shard").get(schema_name=schema_name)

    def apply(self, args=None, kwargs=None, *a, **kw):     # eager / ALWAYS_EAGER
        kw["headers"] = headers_with_schema(kw.get("headers") or {})
        return super().apply(args, kwargs, *a, **kw)
