"""StandaloneCanaryE2E (deploy/migrations_plan_rev7.md §20.4): with USE_MULTITENANT=0 the
canary — including 0002, the §8.2 CIC recipe — is applied by Django's own `migrate` while
the test database is created. Standalone has no migrator and no lock_timeout:
LockTimeoutRestore resets to the server default, which 0003 records.

    USE_MULTITENANT=0 DJANGO_SETTINGS_MODULE=tenants.tests.settings_db_standalone \\
        ../p_env/bin/python manage.py test tenants.tests.db_standalone_canary
"""
import unittest

from django.apps import apps
from django.conf import settings
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase


@unittest.skipUnless(not settings.USE_MULTITENANT and apps.is_installed("tenants.tests.canary"),
                     "needs USE_MULTITENANT=0 and tenants.tests.settings_db_standalone")
class StandaloneCanaryE2E(TransactionTestCase):
    def q(self, sql: str):
        with connection.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]

    def test_canary_applied_by_plain_migrate(self) -> None:
        applied = {n for a, n in MigrationRecorder(connection).applied_migrations() if a == "canary"}
        self.assertEqual(applied, {"0001_initial", "0002_name_index_concurrently",
                                   "0003_probe_lock_timeout", "0004_item_extra"})
        self.assertEqual(self.q("SELECT value FROM canary_probe"), self.q("SELECT reset_val FROM "
                         "pg_settings WHERE name = 'lock_timeout'"))
        self.assertTrue(self.q("SELECT i.indisvalid FROM pg_index i JOIN pg_class c"
                               " ON c.oid = i.indexrelid WHERE c.relname = 'canary_item_name_0002_idx'"))
        self.assertEqual(self.q("SELECT count(*) FROM information_schema.columns"
                                " WHERE table_name = 'canary_item' AND column_name = 'extra'"), 1)
