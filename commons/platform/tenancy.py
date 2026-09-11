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
    from django_tenants.utils import get_public_schema_name
    from tenants.context import schema_context, tenant_context, use_alias

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

    def active_target_schemas(scope: str = "tenants") -> list[str]:
        # No tenants in standalone; the fanout dispatcher is not used here.
        return []

    def active_tenants_with_tz() -> list[tuple[str, str]]:
        return []


__all__ = [
    "schema_context", "tenant_context", "use_alias",
    "get_public_schema_name", "active_target_schemas", "active_tenants_with_tz",
]
