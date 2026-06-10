"""Shard+schema-aware TenantTask — multi-DB adaptation of
tenant_schemas_celery.task.

Carries only `_schema_name` in the message headers (like upstream); the shard
is resolved on the worker from Tenant.shard via get_tenant_for_schema. The
schema is read from the ACTIVE shard connection, not `default`. The actual
switch/restore happens in app.py via task_prerun/postrun.
"""
import copy
from typing import Optional

from .cache import SimpleCache
from .compat import current_schema_name

# Celery >= 5.4 ships DjangoTask, which closes DB connections after each task.
try:
    from celery.contrib.django.task import DjangoTask
    BaseTask = DjangoTask
except ImportError:                       # Celery < 5.4 → add close_old_connections signals
    from celery import Task
    BaseTask = Task

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


class TenantTask(BaseTask):
    abstract = True
    tenant_cache_seconds = None

    @classmethod
    def tenant_cache(cls):
        return SharedTenantCache()

    @classmethod
    def get_tenant_for_schema(cls, schema_name):
        """schema -> Tenant (with .shard), cached for tenant_cache_seconds."""
        from tenants.models import Tenant

        missing = object()
        cache = cls.tenant_cache()
        tenant = cache.get(schema_name, default=missing)
        if tenant is not missing:
            return tenant
        seconds = cls.tenant_cache_seconds
        if seconds is None:
            seconds = int(getattr(cls.app.conf, "task_tenant_cache_seconds", 0) or 0)
        tenant = Tenant.objects.select_related("shard").get(schema_name=schema_name)
        cache.set(schema_name, tenant, expire_seconds=seconds)
        return tenant

    def apply(self, args=None, kwargs=None, *a, **kw):     # eager / ALWAYS_EAGER
        kw["headers"] = headers_with_schema(kw.get("headers") or {})
        return super().apply(args, kwargs, *a, **kw)
