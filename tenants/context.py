"""ContextVar carrying the active DB alias + shard-aware context managers.

current_db      - read by TenantDatabaseRouter on every ORM call. Set per
                  request by TenantShardRoutingMiddleware, or by the helpers
                  below for shell / management-command / admin-action code.
use_alias       - raw alias switch (no schema) for code that manages the
                  schema itself.
schema_context  - DROP-IN replacements for the django-tenants helpers of the
tenant_context    same names. The upstream versions switch ONLY the schema on
                  one connection (by default the 'default' one) and know
                  nothing about our router axis (current_db). For a sharded
                  tenant that sends the ORM to the default DB while the schema
                  is set elsewhere - the off-request variant of the same bug
                  the request path had. These versions wire BOTH axes and
                  restore both on exit.

Import rule: project code imports these from `tenants.context`, never from
`django_tenants.utils`. TenantsConfig.ready() additionally monkeypatches the
upstream module so late importers get these versions too.
"""

from contextlib import ContextDecorator, contextmanager
from contextvars import ContextVar

from django.db import connections
from django_tenants.utils import get_public_schema_name

current_db: ContextVar[str] = ContextVar("current_db", default="default")


@contextmanager
def use_alias(alias: str):
    """Set current_db for the duration of the with-block (no schema change).

    Use when the code manages the schema itself (e.g. raw cursors, DBA flows).
    """
    token = current_db.set(alias)
    try:
        yield
    finally:
        current_db.reset(token)


class tenant_context(ContextDecorator):
    """Drop-in replacement for django_tenants.utils.tenant_context.

    Wires BOTH axes of multi-DB multi-tenancy:
      axis 1: current_db -> the tenant's shard (read by TenantDatabaseRouter);
      axis 2: the tenant schema on THAT shard's connection.

    Also fixes an upstream restore quirk: upstream saves the previous tenant of
    the DEFAULT connection but restores it onto the target one; we save/restore
    the target connection's own previous state.
    """

    def __init__(self, tenant, database=None):
        self.tenant = tenant
        self.database = database or tenant.shard.alias

    def __enter__(self):
        self.connection = connections[self.database]
        self._prev_tenant = self.connection.tenant        # previous of THIS connection
        self._token = current_db.set(self.database)       # axis 1: router
        self.connection.set_tenant(self.tenant)           # axis 2: schema on the shard conn
        return self

    def __exit__(self, *exc):
        if self._prev_tenant is None:
            self.connection.set_schema_to_public()
        else:
            self.connection.set_tenant(self._prev_tenant)
        current_db.reset(self._token)
        return False


class schema_context(ContextDecorator):
    """Drop-in replacement for django_tenants.utils.schema_context.

    Resolves WHICH database to target from the tenant registry
    (schema_name -> Tenant.shard.alias) unless `database=` is given explicitly.
    `public` short-circuits to 'default' without a lookup. Prefer
    tenant_context(tenant) when you already hold the Tenant object - it skips
    the registry query.
    """

    def __init__(self, schema_name, database=None):
        self.schema_name = schema_name
        self.database = database

    def _resolve_database(self):
        if self.database:
            return self.database
        if self.schema_name == get_public_schema_name():
            return "default"
        from tenants.models import Tenant          # lazy: no app-registry cycles
        try:
            return (
                Tenant.objects.select_related("shard")
                .get(schema_name=self.schema_name)
                .shard.alias
            )
        except Tenant.DoesNotExist:
            raise Tenant.DoesNotExist(
                f"schema_context({self.schema_name!r}): no Tenant with this "
                f"schema_name. Pass database=<alias> explicitly for "
                f"non-registered schemas (DBA/restore flows)."
            )

    def __enter__(self):
        self.database = self._resolve_database()
        self.connection = connections[self.database]
        self._prev_tenant = self.connection.tenant
        self._token = current_db.set(self.database)
        self.connection.set_schema(self.schema_name)
        return self

    def __exit__(self, *exc):
        if self._prev_tenant is None:
            self.connection.set_schema_to_public()
        else:
            self.connection.set_tenant(self._prev_tenant)
        current_db.reset(self._token)
        return False
