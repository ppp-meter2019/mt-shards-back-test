"""TTL cache for schema_name -> Tenant lookups. Generic; nothing tenant/shard
specific (mirrors tenant_schemas_celery.cache).

This is the WORKER-LOCAL (per-process) L1 in the schema-resolution tiering
(global schema-snap → this L1 → DB); see tenants.celery.task.get_tenant_for_schema.
"""
from datetime import datetime, timedelta, timezone


def _now():
    return datetime.now(timezone.utc)


class _CacheEntry:
    def __init__(self, key, value, expires_at):
        self.key, self.value, self.expires_at = key, value, expires_at


class SimpleCache:
    # Opportunistic purge threshold: once the store grows past this many entries, a set()
    # drops every expired entry in one pass — so a process that saw many one-off schemas that
    # are never get()-again (hence never evicted on access) can't hold them forever. The store
    # is shared per worker process, so this size trigger works across the per-call instances.
    _PURGE_AT = 1024

    def __init__(self, storage=None):
        self.__items = storage if storage is not None else {}

    def get(self, key, default):
        item = self.__items.get(key)
        if item is None:
            return default
        if item.expires_at < _now():
            self.__items.pop(key, None)   # evict on expiry → bounds growth of re-seen keys
            return default
        return item.value

    def set(self, key, value, expire_seconds):
        items = self.__items
        if len(items) >= self._PURGE_AT:              # opportunistic sweep of expired entries
            now = _now()
            for k in [k for k, e in items.items() if e.expires_at < now]:
                del items[k]
        items[key] = _CacheEntry(key, value, _now() + timedelta(seconds=expire_seconds))
