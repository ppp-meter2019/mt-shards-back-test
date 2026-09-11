"""Re-export OUR shard-aware tenant context + a helper to read the caller's
schema from the ACTIVE shard connection.

Upstream tenant_schemas_celery.compat imports schema_context/tenant_context from
django_tenants.utils (single DB). We use the shard-aware versions from
tenants.context, and read the current schema from connections[current_db], not
from `default`.
"""
from django.db import connections
from django_tenants.utils import get_public_schema_name, get_tenant_model

from tenants.context import active_alias, schema_context, tenant_context, use_alias  # shard-aware

__all__ = [
    "get_public_schema_name", "get_tenant_model",
    "schema_context", "tenant_context", "use_alias", "current_schema_name",
]


def current_schema_name() -> str:
    """Schema on the connection of the CURRENT shard (active_alias → default if unset).

    Fails LOUD on an unexpected error (a bad alias / connection state = a bug): silently
    returning public would mis-stamp the outgoing task and dispatch a tenant task to
    default.public. The legit 'schema unset' case is the `or public` below, NOT a swallowed
    exception. MT-only (standalone runs a plain Celery app that never imports this)."""
    return connections[active_alias()].schema_name or get_public_schema_name()
