"""Tenant + shard routing middleware (sync).

Two cooperating middlewares wire BOTH axes of multi-DB multi-tenancy. They are
SYNC on purpose: the schema must be set on the SAME connection (same thread)
that the ORM query later uses, so the schema-setting cannot live on an async
event-loop middleware. See README "Architecture trade-offs".

  A. ShardAwareTenantMiddleware - subclasses django-tenants' TenantMainMiddleware.
     Resolves the tenant from the Host (pulling its shard in the same query) and
     sets request.tenant + the schema on the DEFAULT connection. Shared models
     (Tenant/Domain/public users) resolve on default.public via the search_path
     public-fallback.

  B. TenantShardRoutingMiddleware - reads request.tenant and wires the SHARD:
     - axis 1 (which DB):     sets the current_db ContextVar the router reads;
     - axis 2 (which schema): sets the tenant schema on the SHARD connection,
       and RESETS it on the way out. The reset is critical for isolation: shard
       connections are persistent (CONN_MAX_AGE) and shared by every tenant on
       that shard - without the reset, the next request for a different tenant
       on the same shard would inherit this search_path and read its data.

     Must be listed AFTER ShardAwareTenantMiddleware (it needs request.tenant).
"""

from django.db import connections
from django_tenants.middleware.main import TenantMainMiddleware

from .context import current_db


class ShardAwareTenantMiddleware(TenantMainMiddleware):
    """TenantMainMiddleware that pulls the tenant's shard in the same query, so
    TenantShardRoutingMiddleware can read request.tenant.shard without an extra
    round-trip."""

    def get_tenant(self, domain_model, hostname):
        return (
            domain_model.objects
            .select_related("tenant__shard")
            .get(domain=hostname)
            .tenant
        )


class TenantShardRoutingMiddleware:
    """Routes the ORM to the tenant's shard and sets that shard connection's
    schema, resetting both on the way out."""

    sync_capable = True
    async_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        alias = tenant.shard.alias if tenant is not None else "default"
        token = current_db.set(alias)                     # axis 1: router -> shard DB
        switched = alias != "default"
        if switched:
            connections[alias].set_tenant(tenant)         # axis 2: search_path on the shard conn
        try:
            return self.get_response(request)
        finally:
            if switched:
                connections[alias].set_schema_to_public()  # reset - prevents cross-tenant leak
            current_db.reset(token)
