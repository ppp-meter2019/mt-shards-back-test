"""hostname -> Aurora alias resolution middleware.

Two interchangeable implementations are provided, one for each worker model:

  * DynamicDatabaseMiddleware     - async, used under Gunicorn + UvicornWorker
  * DynamicDatabaseMiddlewareSync - sync,  used under Gunicorn + gthread/sync

Both classes read from Redis cache first (`tenant_alias` cache), and on miss
query Domain.objects (which the router sends to the default database because
`tenants` is a SHARED-only app). The resolved alias is stored in the
`current_db` ContextVar, which the router consults on every db_for_read /
db_for_write call.

To switch worker model, change the entry in settings.MIDDLEWARE and the
gunicorn --worker-class flag; nothing else in the codebase needs to change.
"""

from asgiref.sync import markcoroutinefunction
from django.core.cache import caches

from .context import current_db

_alias_cache = caches["tenant_alias"]
ALIAS_TTL_HIT  = 3600   # 1 hour for resolved hosts
ALIAS_TTL_MISS = 30     # 30 seconds for unknown hosts (so new tenants surface quickly)


# ---------------------------------------------------------------------------
# Async (current default) - paired with Gunicorn UvicornWorker / ASGI.
# ---------------------------------------------------------------------------

class DynamicDatabaseMiddleware:
    async_capable = True
    sync_capable  = False

    def __init__(self, get_response):
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request):
        host  = request.get_host().split(":")[0]
        alias = await self._resolve(host)
        token = current_db.set(alias)
        try:
            return await self.get_response(request)
        finally:
            current_db.reset(token)

    async def _resolve(self, host: str) -> str:
        key = f"tenant_alias:{host}"
        alias = await _alias_cache.aget(key)
        if alias:
            return alias

        # Cache miss -> query the default database. Imported lazily to avoid
        # touching the apps registry at module-import time.
        from tenants.models import Domain
        try:
            d = await (Domain.objects
                       .select_related("tenant__shard")
                       .only("tenant__shard__alias")
                       .aget(domain=host))
            alias = d.tenant.shard.alias or "default"
            ttl = ALIAS_TTL_HIT
        except Domain.DoesNotExist:
            alias = "default"
            ttl = ALIAS_TTL_MISS

        await _alias_cache.aset(key, alias, ttl)
        return alias


# ---------------------------------------------------------------------------
# Sync - paired with Gunicorn gthread/sync worker / WSGI.
#
# Identical logic to the async version above, just without the coroutine
# machinery. ContextVar isolation is per-thread under gthread workers, so the
# current_db.set/reset pattern still gives correct request-scoped state.
# ---------------------------------------------------------------------------

class DynamicDatabaseMiddlewareSync:
    async_capable = False
    sync_capable  = True

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host  = request.get_host().split(":")[0]
        alias = self._resolve(host)
        token = current_db.set(alias)
        try:
            return self.get_response(request)
        finally:
            current_db.reset(token)

    def _resolve(self, host: str) -> str:
        key = f"tenant_alias:{host}"
        alias = _alias_cache.get(key)
        if alias:
            return alias

        from tenants.models import Domain
        try:
            d = (Domain.objects
                 .select_related("tenant__shard")
                 .only("tenant__shard__alias")
                 .get(domain=host))
            alias = d.tenant.shard.alias or "default"
            ttl = ALIAS_TTL_HIT
        except Domain.DoesNotExist:
            alias = "default"
            ttl = ALIAS_TTL_MISS

        _alias_cache.set(key, alias, ttl)
        return alias
