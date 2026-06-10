"""Re-export OUR shard-aware tenant context + a helper to read the caller's
schema from the ACTIVE shard connection.

Upstream tenant_schemas_celery.compat imports schema_context/tenant_context from
django_tenants.utils (single DB). We use the shard-aware versions from
tenants.context, and read the current schema from connections[current_db], not
from `default`.
"""
from django.db import connections
from django_tenants.utils import get_public_schema_name, get_tenant_model

from tenants.context import current_db, schema_context, tenant_context  # shard-aware

__all__ = [
    "get_public_schema_name", "get_tenant_model",
    "schema_context", "tenant_context", "current_schema_name",
]


def current_schema_name():
    """Schema set on the connection of the CURRENT shard (current_db)."""
    alias = current_db.get()
    try:
        return connections[alias].schema_name or get_public_schema_name()
    except Exception:
        return get_public_schema_name()
