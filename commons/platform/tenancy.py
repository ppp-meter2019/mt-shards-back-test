"""Mode-agnostic tenancy primitives — the ONE place USE_MULTITENANT is branched for
application code.

Import these from application / business code INSTEAD of importing `tenants` (or
`django_tenants`) directly, so the same code runs in both modes:

  * multitenant  -> the real shard-aware helpers from the `tenants` app;
  * standalone   -> no-ops (there is a single default DB, no schemas to switch).

In standalone the `tenants` app is not installed, so this module must NOT import it
there — hence the branch. Business code stays import-clean and works in both modes.
"""
from collections.abc import Iterator
from typing import Any

from django.conf import settings

if settings.USE_MULTITENANT:
    # get_public_schema_name through tenants.context, not django_tenants.utils directly: one
    # chain to the upstream helper instead of two routes to it. tenants.context defers the
    # upstream import to call time and caches it, which costs ~46 ns/call (measured) -- and
    # make_key, the only hot caller, does not pay it at all: `schema_name or
    # get_public_schema_name()` short-circuits whenever a context is bound.
    from tenants.context import (active_alias, bound_alias, current_schema_name,
                                 get_public_schema_name, schema_context, tenant_context,
                                 use_alias)

    def active_target_schemas(scope: str = "tenants") -> list[str]:
        """ACTIVE tenant schemas for the interval fanout (excludes the public schema).

        Read from default.public in one query. Used by tenants.tasks.fanout_dispatch;
        `scope="public"` never reaches here (it is a passthrough in scoped_schedule)."""
        from tenants.models import Tenant
        return list(
            Tenant.objects.filter(status=Tenant.Status.ACTIVE)
            .exclude(schema_name=get_public_schema_name())
            .values_list("schema_name", flat=True)
        )

    def active_tenants_with_tz() -> list[tuple[str, str]]:
        """(schema, timezone) for ACTIVE tenants that HAVE a configured tz — the calendar
        (tz) fanout targets. Excludes public and NULL-tz tenants (the latter are skipped
        until their in-schema singleton sets a timezone). One query from default.public."""
        from tenants.models import Tenant
        return list(
            Tenant.objects.filter(status=Tenant.Status.ACTIVE, timezone__isnull=False)
            .exclude(schema_name=get_public_schema_name())
            .values_list("schema_name", "timezone")
        )
else:
    from contextlib import contextmanager

    @contextmanager
    def schema_context(*args: Any, **kwargs: Any) -> Iterator[None]:
        # No schemas in standalone — run the body against the single default DB.
        yield

    @contextmanager
    def tenant_context(*args: Any, **kwargs: Any) -> Iterator[None]:
        yield

    @contextmanager
    def use_alias(*args: Any, **kwargs: Any) -> Iterator[None]:
        yield

    def get_public_schema_name() -> str:
        return "public"

    def current_schema_name() -> str:
        # `public`, not "": the same string get_public_schema_name() returns here, so shared
        # business code comparing the two behaves identically in both modes. Returning ""
        # made `current_schema_name() == get_public_schema_name()` True under MT-without-
        # context and False here -- one name, two meanings, and neither wrong on its own.
        return get_public_schema_name()

    def active_alias() -> str:
        # No shards in standalone: there is one connection and it is `default`.
        return "default"

    def bound_alias() -> str | None:
        # Standalone has one connection and no routing axis to bind, so "unbound" is not a
        # distinguishable state here — unlike MT, where None means nobody established context.
        return "default"

    def active_target_schemas(scope: str = "tenants") -> list[str]:
        # No tenants in standalone; the fanout dispatcher is not used here.
        return []

    def active_tenants_with_tz() -> list[tuple[str, str]]:
        return []


__all__ = [
    "schema_context", "tenant_context", "use_alias", "active_alias", "bound_alias",
    "get_public_schema_name", "current_schema_name",
    "active_target_schemas", "active_tenants_with_tz",
]
