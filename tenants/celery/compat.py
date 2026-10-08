"""Re-export OUR shard-aware tenant context + a helper to read the caller's
schema from the ACTIVE shard connection.

Upstream tenant_schemas_celery.compat imports schema_context/tenant_context from
django_tenants.utils (single DB). We use the shard-aware versions from
tenants.context, and read the current schema from connections[current_db], not
from `default`.
"""
# Everything here comes from tenants.context, which defers the django_tenants.utils import to
# call time; see the docstring there. Importing from django_tenants.utils HERE would re-arm
# exactly what that defers, because this module is on the import path of
# tenants_back/__init__.py and therefore runs mid-settings-load.
#
# current_schema_name lives in tenants.context (which owns `connections` + bound_alias) rather
# than being spelled out again here: this module's copy was byte-identical to
# commons.platform.cache_keys.current_schema, and two copies of a tenant-identity read is one
# too many.
from tenants.context import (  # shard-aware
    current_schema_name, get_public_schema_name, schema_context, tenant_context, use_alias,
)

__all__ = [
    "get_public_schema_name",
    "schema_context", "tenant_context", "use_alias", "current_schema_name",
]
