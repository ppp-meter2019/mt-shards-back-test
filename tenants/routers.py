"""Multi-DB database router.

Inherits django-tenants TenantSyncRouter:
  - db_for_read/write: picks the Aurora alias from the current_db ContextVar, and refuses a
                       tenant-model query aimed at the PUBLIC schema (see _guard_public).
  - allow_migrate:    keeps upstream logic (app_in_list, multi-type, schema-based),
                      replaces ONLY the single-DB guard with our multi-DB guard, and skips
                      the closure apps' data migrations on public.
"""

import logging
from typing import Any

from django.conf import settings
from django.db import connections
from django.db.models import Model
from django.db.utils import ConnectionDoesNotExist
from django_tenants.routers import TenantSyncRouter
from django_tenants.utils import (
    get_public_schema_name,
    get_tenant_types,
    has_multi_type_tenants,
)

from .context import bound_alias

logger = logging.getLogger(__name__)


class PublicSchemaModelDenied(RuntimeError):
    """A tenant model was queried while the target connection is on the PUBLIC schema.

    Subclasses RuntimeError for the same reason the strict-context guard raises one: it is a
    BUG, and the two belong to the same family. Deliberately NOT a ProgrammingError and NOT
    an AttributeError — either would be swallowed by defensive code that exists to survive a
    missing table or a missing attribute, which is exactly where this must not be silent.
    """


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
        # The connection this query WOULD reach — "default" is where an unset context lands
        # (that is the premise of the strict guard below), so the public check applies to
        # both cases and runs FIRST on purpose. The two refusals answer different questions,
        # and on the public schema only one of them is true: there is no shard to bind, so
        # "establish a routing context" would send the reader looking for a bug that is not
        # there. Off public the check is a no-op and the strict guard speaks, which is right
        # — during a TENANT request django-tenants has already set that tenant's schema on
        # `default`, so this correctly stays quiet there.
        self._guard_public(model, alias or "default")
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

    @staticmethod
    def _guard_public(model: type[Model], alias: str) -> None:
        """Refuse a tenant-model query whose target connection sits on the PUBLIC schema.

        The closure apps have TABLES in public so the identity model can be created there,
        and those tables are EMPTY. Without this the ORM would answer such a query with zero
        rows — a plausible-looking wrong answer, which is the failure mode this whole design
        exists to avoid. (For a business app outside the closure there is no table at all, so
        the alternative is a raw ProgrammingError; a named refusal is simply a better one.)
        It fires on the management host, where the middleware pins use_alias("default"), on a
        Celery task dispatched with _schema_name=public, and — via the caller passing
        "default" — on a query made with NO routing context at all (a shell, a management
        command run without tenant_command).

        Scope is TENANT_STRICT_ROUTE_APPS, reusing the distinction that setting already
        draws: contenttypes / auth / admin are tenant apps too, but Django queries them on
        public legitimately (admin, permissions, the operator's own session), so they are out
        of it by design — see the note there. Exempt on top of that are the models named in
        PUBLIC_MODEL_ALLOWLIST: the operator's identity and whatever its save path touches.

        Mode comes from settings.PUBLIC_MODEL_GUARD:
          "raise" (default) — what a complete allowlist deserves;
          "warn"            — log and continue. This is how the allowlist gets MEASURED at
                              merge: run createsuperuser / login / the admin against public
                              and read the log instead of guessing;
          "off"             — skip the check entirely.
        """
        mode = getattr(settings, "PUBLIC_MODEL_GUARD", "raise")
        if mode == "off":
            return
        try:
            schema = connections[alias].schema_name
        except (ConnectionDoesNotExist, AttributeError):
            # An alias with no connection fails at execute anyway, and a plain (non
            # django-tenants) backend has no schema — neither is this guard's business.
            return
        if schema != get_public_schema_name():
            return
        label = model._meta.app_label
        if label not in getattr(settings, "TENANT_STRICT_ROUTE_APPS", frozenset()):
            return
        if model._meta.label_lower in getattr(settings, "PUBLIC_MODEL_ALLOWLIST", frozenset()):
            return
        message = (
            f"{model._meta.label}: tenant-model query on the PUBLIC schema (alias {alias!r}). "
            f"Its table is either absent there or deliberately EMPTY, so the query cannot "
            f"return a meaningful answer. If this model legitimately holds rows in public "
            f"(the operator's identity and what its save path touches), add "
            f"{model._meta.label_lower!r} to settings_base._PUBLIC_MODEL_ALLOWLIST; otherwise "
            f"this is a business query that escaped onto the management host."
        )
        if mode == "warn":
            logger.warning(message, stack_info=True)
            return
        raise PublicSchemaModelDenied(message)

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

        # DATA migrations of the per-tenant apps must never run on the public schema. They
        # are in SHARED_APPS so that every FK target exists when the identity table is created
        # there; every RunPython/RunSQL they carry seeds or rewrites BUSINESS rows, which is
        # the one thing public must not get.
        #
        # Keyed on TENANT_STRICT_ROUTE_APPS — the per-tenant app set. Its name records its
        # first consumer, but all three router behaviours police the same apps, and splitting
        # the name would only invite the three to drift.
        #
        # `model_name is None` is what identifies them, and it is the only signal available:
        # schema operations reach a router through allow_migrate_model(), which always passes
        # the model (django/db/utils.py), while RunPython/RunSQL call
        # allow_migrate(alias, app_label, **hints) with no model at all
        # (django/db/migrations/operations/special.py). So this filter CANNOT be per-model —
        # it is per-app by construction. That is why the models which legitimately hold rows
        # in public are a SEPARATE list (settings.PUBLIC_MODEL_ALLOWLIST) applied at QUERY
        # time, not here — and why that list is now the ONLY thing the merge has to decide.
        #
        # Caveat, documented rather than worked around: CreateExtension / CreateCollation /
        # RemoveCollation (django/contrib/postgres/operations.py) also arrive without a model
        # and are therefore skipped on public too. A closure app must not create a
        # schema-level object its own public tables depend on — hoist that into a genuinely
        # shared app's migration. The host project has no such operation today.
        if (model_name is None
                and connection.schema_name == public_schema_name
                and app_label in getattr(settings, "TENANT_STRICT_ROUTE_APPS", frozenset())):
            return False

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
