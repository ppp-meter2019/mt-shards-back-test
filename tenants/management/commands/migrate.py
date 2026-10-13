"""`manage.py migrate` is disabled in multi-tenant mode (deploy/migrations_plan_rev7.md §12).

Upstream django_tenants maps `migrate` to its migrate_schemas (no lease, no lock_timeout,
no status handling, 503 for nobody). Tenants and public are migrated only by our
`migrate_schemas`. The one legitimate caller is Django's test-database creation, let
through by tenants.test_runner.TenantsTestRunner; it then runs exactly what it ran before
this override (upstream MigrateSchemasCommand, whose argument parser we keep).

Wins over django_tenants' `migrate` because `tenants` precedes `django_tenants` in
SHARED_APPS. In standalone `tenants` is not installed, so `migrate` is Django's own.
"""
from typing import Any

from django.core.management.base import CommandError
from django_tenants.management.commands.migrate_schemas import MigrateSchemasCommand

from commons.platform.commands import TenantCommand


# TenantCommand FIRST in the MRO: its execute() runs the USE_MULTITENANT guard.
class Command(TenantCommand, MigrateSchemasCommand):
    help = ("Disabled in multi-tenant mode: use `migrate_schemas` "
            "(deploy/migrations_plan_rev7.md §12). Allowed only while tests create their databases.")

    def handle(self, *args: Any, **options: Any) -> None:
        from tenants import test_runner
        if not test_runner._creating_test_dbs:
            raise CommandError("manage.py migrate is disabled in multi-tenant mode; use "
                               "migrate_schemas (see deploy/migrations_zero_downtime.md).")
        return super().handle(*args, **options)
