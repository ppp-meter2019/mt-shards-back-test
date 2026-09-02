"""Management commands: warm_resolve_cache / invalidate_resolve_cache. DB-free —
resolve_cache methods and redis_alive are mocked."""
from unittest import mock

from django.core.management import CommandError, call_command, load_command_class
from django.test import SimpleTestCase, override_settings

import tenants.resolver as rc_mod
from commons.platform.commands import TenantCommand


# Every tenant-management command must be a TenantCommand (Phase 4). This list is
# the guard's coverage contract — a new tenant command must be added here.
TENANT_COMMANDS = [
    "bootstrap_public",
    "bootstrap_tenant",
    "create_tenant_schema",
    "drop_tenant_schema",
    "invalidate_resolve_cache",
    "migrate_schemas",
    "reconcile_tenants",
    "sync_shards",
    "warm_resolve_cache",
]


class TenantCommandGuardTests(SimpleTestCase):
    """USE_MULTITENANT guard on tenant-management commands (commons.platform.commands)."""

    def test_all_tenant_commands_are_tenant_commands(self):
        for name in TENANT_COMMANDS:
            cmd = load_command_class("tenants", name)
            self.assertIsInstance(
                cmd, TenantCommand,
                f"{name} must subclass TenantCommand (see commons.platform.commands)",
            )

    @override_settings(USE_MULTITENANT=False)
    def test_refused_in_standalone(self):
        # The guard is the first thing execute() does, so it raises before any
        # argument/DB work — calling execute() with no options is enough.
        for name in TENANT_COMMANDS:
            cmd = load_command_class("tenants", name)
            with self.assertRaises(CommandError, msg=name) as ctx:
                cmd.execute()
            self.assertIn("USE_MULTITENANT", str(ctx.exception), name)


class WarmCommandTests(SimpleTestCase):
    def test_errors_when_redis_down(self):
        with mock.patch.object(rc_mod.resolve_cache, "redis_alive", return_value=False):
            with self.assertRaises(CommandError):
                call_command("warm_resolve_cache")

    def test_fill_and_force(self):
        with mock.patch.object(rc_mod.resolve_cache, "redis_alive", return_value=True), \
             mock.patch.object(rc_mod.resolve_cache, "warm", return_value=3) as warm:
            call_command("warm_resolve_cache")
            self.assertFalse(warm.call_args.kwargs.get("force"))
            call_command("warm_resolve_cache", force=True)
            self.assertTrue(warm.call_args.kwargs.get("force"))


class InvalidateCommandTests(SimpleTestCase):
    def test_errors_when_redis_down(self):
        with mock.patch.object(rc_mod.resolve_cache, "redis_alive", return_value=False):
            with self.assertRaises(CommandError):
                call_command("invalidate_resolve_cache", all=True)

    def test_requires_a_target(self):
        with mock.patch.object(rc_mod.resolve_cache, "redis_alive", return_value=True):
            with self.assertRaises(CommandError):
                call_command("invalidate_resolve_cache")

    def test_dispatch_to_ids_names_all(self):
        with mock.patch.object(rc_mod.resolve_cache, "redis_alive", return_value=True), \
             mock.patch.object(rc_mod.resolve_cache, "forget_ids", return_value=2) as fi, \
             mock.patch.object(rc_mod.resolve_cache, "forget_schemas", return_value=1) as fn, \
             mock.patch.object(rc_mod.resolve_cache, "forget_all", return_value=9) as fa:
            call_command("invalidate_resolve_cache", ids=[1, 2])
            fi.assert_called_once()
            call_command("invalidate_resolve_cache", schemas=["alpha"])
            fn.assert_called_once()
            call_command("invalidate_resolve_cache", all=True)
            fa.assert_called_once()