"""Test runner for multi-tenant mode (deploy/migrations_plan_rev7.md §12).

`manage.py migrate` is disabled in MT (tenants/management/commands/migrate.py): it would
run upstream migrate_schemas with no lease, no lock_timeout and no status handling. Django's
test-database creation still calls ``call_command("migrate", …)``
(django/db/backends/base/creation.py, create_test_db); this runner raises a flag around
exactly that, and the override lets it through.
"""
from typing import Any

from django.test.runner import DiscoverRunner

_creating_test_dbs = False


class TenantsTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs: Any) -> Any:
        global _creating_test_dbs
        _creating_test_dbs = True
        try:
            return super().setup_databases(**kwargs)
        finally:
            _creating_test_dbs = False
