"""TTL cache for schema_name -> Tenant lookups. Generic; nothing tenant/shard
specific (mirrors tenant_schemas_celery.cache)."""
from datetime import datetime, timedelta


class _CacheEntry:
    def __init__(self, key, value, expires_at):
        self.key, self.value, self.expires_at = key, value, expires_at


class SimpleCache:
    def __init__(self, storage=None):
        self.__items = storage if storage is not None else {}

    def get(self, key, default):
        item = self.__items.get(key)
        if item is None or item.expires_at < datetime.utcnow():
            return default
        return item.value

    def set(self, key, value, expire_seconds):
        self.__items[key] = _CacheEntry(
            key, value, datetime.utcnow() + timedelta(seconds=expire_seconds),
        )
