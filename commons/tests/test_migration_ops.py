"""commons.platform.migration_ops (deploy/migrations_plan_rev7.md §8.2) and the
standalone-mode contract of the migrator layer (§20.2 StandaloneTests). Runs in BOTH CI
modes (scripts/ci_mode.sh lists `commons` for standalone); the standalone class skips
under USE_MULTITENANT=1, its MT mirror lives in tenants/tests/test_migrate_override.py.
"""
import unittest
from unittest import mock

from django.conf import settings
from django.core.management import get_commands
from django.db import migrations
from django.test import SimpleTestCase, override_settings

from commons.platform import migration_ops
from commons.platform.migration_ops import LockTimeoutOff, LockTimeoutRestore, _restore


class _Editor:
    """A schema_editor double: records what _restore executes."""

    def __init__(self) -> None:
        self.sql: list[tuple] = []
        cursor = mock.MagicMock()
        cursor.__enter__.return_value.execute.side_effect = lambda *a: self.sql.append(a)
        self.connection = mock.Mock(cursor=mock.Mock(return_value=cursor))


class MigrationOpsTests(SimpleTestCase):
    def test_deconstruct_has_no_arguments(self) -> None:
        self.assertEqual(LockTimeoutOff().deconstruct(),
                         ("commons.platform.migration_ops.LockTimeoutOff", [], {}))
        self.assertEqual(LockTimeoutRestore().deconstruct(),
                         ("commons.platform.migration_ops.LockTimeoutRestore", [], {}))

    def test_operations(self) -> None:
        off = LockTimeoutOff()
        self.assertIsInstance(off, migrations.RunSQL)
        self.assertEqual(off.sql, "SET lock_timeout = 0")
        self.assertEqual(off.reverse_sql, migrations.RunSQL.noop)
        restore = LockTimeoutRestore()
        self.assertIs(restore.code, _restore)
        self.assertIs(restore.reverse_code, migrations.RunPython.noop)

    @override_settings(TENANT_MIGRATE_DDL_LOCK_TIMEOUT=None)
    def test_restore_none_is_server_default(self) -> None:
        ed = _Editor()
        _restore(None, ed)
        self.assertEqual(ed.sql, [("SET lock_timeout TO DEFAULT",)])

    @override_settings(TENANT_MIGRATE_DDL_LOCK_TIMEOUT="5s")
    def test_restore_value_is_bound_not_formatted(self) -> None:
        ed = _Editor()
        _restore(None, ed)
        self.assertEqual(ed.sql, [("SELECT set_config('lock_timeout', %s, false)", ["5s"])])

    def test_module_does_not_import_tenants(self) -> None:
        src = open(migration_ops.__file__, encoding="utf-8").read()
        self.assertNotRegex(src, r"(?m)^\s*(from|import)\s+tenants\b")


@unittest.skipIf(settings.USE_MULTITENANT, "standalone-only (scripts/ci_mode.sh standalone)")
class StandaloneTests(SimpleTestCase):
    def test_migrate_is_djangos(self) -> None:
        self.assertEqual(get_commands()["migrate"], "django.core")

    def test_no_tenants_app_and_no_lock_timeout(self) -> None:
        self.assertNotIn("tenants", settings.INSTALLED_APPS)
        self.assertNotIn("django_tenants", settings.INSTALLED_APPS)
        self.assertIsNone(settings.TENANT_MIGRATE_DDL_LOCK_TIMEOUT)
