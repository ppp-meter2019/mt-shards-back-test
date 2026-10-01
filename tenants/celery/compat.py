"""Re-export OUR shard-aware tenant context + a helper to read the caller's
schema from the ACTIVE shard connection.

Upstream tenant_schemas_celery.compat imports schema_context/tenant_context from
django_tenants.utils (single DB). We use the shard-aware versions from
tenants.context, and read the current schema from connections[current_db], not
from `default`.
"""
from django.db import connections
from django_tenants.utils import get_public_schema_name, get_tenant_model

from tenants.context import bound_alias, schema_context, tenant_context, use_alias  # shard-aware

__all__ = [
    "get_public_schema_name", "get_tenant_model",
    "schema_context", "tenant_context", "use_alias", "current_schema_name",
]


def current_schema_name() -> str:
    """Schema on the connection of the BOUND shard; public when no context is bound.

    Fails LOUD on an unexpected error (a bad alias / connection state = a bug): silently
    returning public would mis-stamp the outgoing task and dispatch a tenant task to
    default.public. MT-only (standalone runs a plain Celery app that never imports this).

    The "no context" case is legitimate and is answered EXPLICITLY, not by reading
    connections["default"]. It is reached on the request path: HostRegistry.trigger_warm()
    (tenants/resolver/registry.py) enqueues a reconcile from inside
    ShardAwareTenantMiddleware, before TenantShardRoutingMiddleware binds the axis. That read
    only HAPPENS to be public today, because upstream resets the schema on the first line of
    process_request and trigger_warm fires before set_tenant(). After set_tenant() the very
    same read stamps the tenant being served onto an unrelated task, and the receiving worker
    (TenantTask.__call__) enters that tenant in full — no error, wrong tenant. Same reasoning
    as commons.platform.cache_keys.current_schema."""
    alias = bound_alias()
    if alias is None:
        return get_public_schema_name()
    return connections[alias].schema_name or get_public_schema_name()
