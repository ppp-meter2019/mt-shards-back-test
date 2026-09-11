"""Multi-DB database router.

Inherits django-tenants TenantSyncRouter:
  - db_for_read/write: picks the Aurora alias from the current_db ContextVar.
  - allow_migrate:    keeps upstream logic (app_in_list, multi-type, schema-based)
                      and replaces ONLY the single-DB guard with our multi-DB guard.
"""

from typing import Any

from django.conf import settings
from django.db import connections
from django.db.models import Model
from django_tenants.routers import TenantSyncRouter
from django_tenants.utils import (
    get_public_schema_name,
    get_tenant_types,
    has_multi_type_tenants,
)

from .context import bound_alias


class TenantDatabaseRouter(TenantSyncRouter):

    def db_for_read(self, model: type[Model], **hints: Any) -> str | None:
        label = model._meta.app_label
        # Shared-only apps (Tenant/Shard/Domain registry, sessions, ...)
        # always live on the default database.
        if (self.app_in_list(label, settings.SHARED_APPS)
                and not self.app_in_list(label, settings.TENANT_APPS)):
            return "default"
        # Otherwise use the alias set by TenantShardRoutingMiddleware / tenant_context / use_alias.
        alias = bound_alias()                  # raw: None == no routing context
        if alias is None:                         # NO routing context established
            if label in getattr(settings, "TENANT_STRICT_ROUTE_APPS", frozenset()):
                raise RuntimeError(
                    f"{model._meta.label}: tenant-model query with NO routing context "
                    f"(current_db unset) — it would silently hit the DEFAULT database (wrong "
                    f"shard). Establish a context: tenant_context(tenant) / use_alias(alias), run "
                    f"the command via `manage.py tenant_command <cmd> --schema=<schema>`, or (on "
                    f"the request path) ensure TenantShardRoutingMiddleware ran."
                )
            return "default"                      # quasi-shared (contenttypes/auth/admin) — benign
        return alias

    def db_for_write(self, model: type[Model], **hints: Any) -> str | None:
        return self.db_for_read(model, **hints)

    def allow_migrate(self, db: str, app_label: str, model_name: str | None = None,
                      **hints: Any) -> bool | None:
        """Combines the upstream schema-based decision with a multi-DB guard.

        Upstream rejects any db != get_tenant_database_alias() (i.e. != 'default'),
        which breaks multi-DB. We keep the rest of upstream's behavior (app_in_list
        with django_cache shortcut and AppConfig-path matching, multi-type tenant
        support) and substitute that single check.
        """
        connection = connections[db]
        public_schema_name = get_public_schema_name()

        if has_multi_type_tenants():
            tenant_types = get_tenant_types()
            if connection.schema_name == public_schema_name:
                installed_apps = tenant_types[public_schema_name]["APPS"]
            else:
                tenant_type = connection.tenant.get_tenant_type()
                installed_apps = tenant_types[tenant_type]["APPS"]
        else:
            if connection.schema_name == public_schema_name:
                installed_apps = settings.SHARED_APPS
            else:
                installed_apps = settings.TENANT_APPS

        if not self.app_in_list(app_label, installed_apps):
            return False

        # Multi-DB guard, replacing upstream's `db != get_tenant_database_alias()`:
        #   - public schema migrations only on 'default'
        #   - tenant schema migrations only on non-'default' shards
        if connection.schema_name == public_schema_name:
            return db == "default"
        return db != "default"
