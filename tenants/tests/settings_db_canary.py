"""settings_db + the canary tenant app (tenants/tests/canary) for the E2E migrator tests
(deploy/migrations_plan_rev7.md §20.1, §20.4). Separate from settings_db because canary
0002 runs CREATE INDEX CONCURRENTLY, which fails inside TestCase's wrapping transaction —
the TestCase suites (db_migrator_core, db_migrate_schemas, db_lease_reconcile) must not
see it. Only TransactionTestCase suites run here:

    DJANGO_SETTINGS_MODULE=tenants.tests.settings_db_canary \\
        ../p_env/bin/python manage.py test tenants.tests.db_migrator_e2e
"""
from tenants.tests.settings_db import *  # noqa: F401,F403
from tenants.tests.settings_db import INSTALLED_APPS, TENANT_APPS

TENANT_APPS = [*TENANT_APPS, "tenants.tests.canary"]
INSTALLED_APPS = [*INSTALLED_APPS, "tenants.tests.canary"]
