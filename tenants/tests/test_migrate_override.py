"""`manage.py migrate` disabled in MT + TenantsTestRunner (deploy/migrations_plan_rev7.md
§12, MigrateOverrideTests) and the migrator settings checks (§13, ChecksTests)."""
import os
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command, get_commands, load_command_class
from django.test import SimpleTestCase, override_settings
from django.test.runner import DiscoverRunner
from django_tenants.management.commands.migrate_schemas import MigrateSchemasCommand

from tenants import test_runner
from tenants.checks.migrate import migrator_settings

OK = dict(TENANT_MIGRATE_JOBS=2, TENANT_MIGRATE_JOBS_PER_SHARD=1,
          TENANT_MIGRATE_DDL_LOCK_TIMEOUT="5s", TENANT_MIGRATE_LOG_DIR="/tmp/x")


class MigrateOverrideTests(SimpleTestCase):
    def test_migrate_comes_from_tenants(self) -> None:
        self.assertEqual(get_commands()["migrate"], "tenants")
        self.assertEqual(settings.TEST_RUNNER, "tenants.test_runner.TenantsTestRunner")

    def test_refused_outside_test_db_creation(self) -> None:
        with mock.patch.object(MigrateSchemasCommand, "handle") as upstream, \
             self.assertRaisesMessage(CommandError, "use migrate_schemas"):
            call_command("migrate")
        upstream.assert_not_called()

    def test_delegates_while_creating_test_dbs(self) -> None:
        with mock.patch.object(test_runner, "_creating_test_dbs", True), \
             mock.patch.object(MigrateSchemasCommand, "handle", return_value=None) as upstream:
            call_command("migrate", "--noinput", database="default")
        upstream.assert_called_once()

    def test_parser_is_upstreams(self) -> None:
        cmd = load_command_class("tenants", "migrate")
        dests = {a.dest for a in cmd.create_parser("manage.py", "migrate")._actions}
        self.assertTrue({"shared", "tenant", "schema_name", "executor", "run_syncdb"} <= dests)

    def test_runner_raises_the_flag_only_around_setup_databases(self) -> None:
        seen = []
        with mock.patch.object(DiscoverRunner, "setup_databases",
                               side_effect=lambda **kw: seen.append(test_runner._creating_test_dbs)):
            test_runner.TenantsTestRunner().setup_databases()
        self.assertEqual((seen, test_runner._creating_test_dbs), ([True], False))
        with mock.patch.object(DiscoverRunner, "setup_databases", side_effect=RuntimeError), \
             self.assertRaises(RuntimeError):
            test_runner.TenantsTestRunner().setup_databases()
        self.assertFalse(test_runner._creating_test_dbs)


class ChecksTests(SimpleTestCase):
    def ids(self, **overrides) -> list[str]:
        with override_settings(**{**OK, **overrides}):
            return [m.id for m in migrator_settings(None)]

    def test_clean(self) -> None:
        self.assertEqual(self.ids(), [])
        self.assertEqual(self.ids(TENANT_MIGRATE_DDL_LOCK_TIMEOUT="0"), [])
        self.assertEqual(self.ids(TENANT_MIGRATE_DDL_LOCK_TIMEOUT="1500ms"), [])
        self.assertEqual(self.ids(TENANT_MIGRATE_DDL_LOCK_TIMEOUT="2min"), [])

    def test_project_settings_are_clean(self) -> None:
        self.assertEqual([m.id for m in migrator_settings(None)], [])

    def test_e020_upstream_multiprocessing(self) -> None:
        self.assertEqual(self.ids(TENANT_MULTIPROCESSING_MAX_PROCESSES=4), ["tenants.E020"])

    def test_w021_executor_env(self) -> None:
        with mock.patch.dict(os.environ, {"EXECUTOR": "multiprocessing"}):
            self.assertEqual(self.ids(), ["tenants.W021"])

    def test_e022_jobs(self) -> None:
        for bad in (0, -1, True, "2", 1.5, None):
            self.assertEqual(self.ids(TENANT_MIGRATE_JOBS=bad), ["tenants.E022"], bad)
        self.assertEqual(self.ids(TENANT_MIGRATE_JOBS_PER_SHARD=False), ["tenants.E022"])
        self.assertEqual(self.ids(TENANT_MIGRATE_JOBS=2, TENANT_MIGRATE_JOBS_PER_SHARD=3),
                         ["tenants.E022"])

    def test_e024_lock_timeout(self) -> None:
        for bad in ("5", "5 s", "5sec", "-1s", "", None, 5, "1h"):
            self.assertEqual(self.ids(TENANT_MIGRATE_DDL_LOCK_TIMEOUT=bad), ["tenants.E024"], bad)

    def test_e029_log_dir(self) -> None:
        for bad in (None, "", 42):
            self.assertEqual(self.ids(TENANT_MIGRATE_LOG_DIR=bad), ["tenants.E029"], bad)

    @override_settings(USE_MULTITENANT=False, TENANT_MIGRATE_JOBS=0)
    def test_mt_only(self) -> None:
        self.assertEqual(migrator_settings(None), [])
