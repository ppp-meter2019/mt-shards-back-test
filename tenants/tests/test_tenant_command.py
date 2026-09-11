"""Shard-aware tenant_command override — runs the wrapped sub-command inside tenant_context,
so its ORM routes to the tenant's SHARD (current_db), not the default DB. DB-free: tenant
resolution is mocked and `connections` is faked (like test_context)."""
from typing import Any
import types
from unittest import mock

from django.test import SimpleTestCase

from tenants.context import bound_alias


class _FakeConn:
    def __init__(self) -> None:
        self.tenant = None

    def set_tenant(self, t: Any) -> None:
        self.tenant = t

    def set_schema(self, name: str) -> None:
        pass

    def set_schema_to_public(self) -> None:
        self.tenant = None


def _tenant(alias: str, schema: str = "acme") -> Any:
    return types.SimpleNamespace(shard=types.SimpleNamespace(alias=alias), schema_name=schema)


class TenantCommandShardTests(SimpleTestCase):
    def test_handle_runs_subcommand_on_the_shard(self) -> None:
        from tenants.management.commands import tenant_command as tc
        seen = {}
        t = _tenant("shard_7")

        def spy(name: str, *a: Any, **kw: Any) -> None:
            seen["db"] = bound_alias()          # where the sub-command's ORM would route
            seen["name"] = name

        cmd = tc.Command()
        with mock.patch.object(tc.Command, "get_tenant_from_options_or_interactive", return_value=t), \
             mock.patch.object(tc, "call_command", side_effect=spy), \
             mock.patch("tenants.context.connections", {"shard_7": _FakeConn()}):
            cmd.handle(command_name=["seed_products"], command_options=[])

        self.assertEqual(seen["name"], "seed_products")
        self.assertEqual(seen["db"], "shard_7")        # routed to the tenant's shard, not default
        self.assertIsNone(bound_alias())            # restored to unset after the command

    def test_run_from_argv_wraps_in_tenant_context(self) -> None:
        from tenants.management.commands import tenant_command as tc
        seen = {}
        t = _tenant("shard_9", schema="beta")

        class _Klass:
            def run_from_argv(self, args: list[str]) -> None:
                seen["db"] = bound_alias()

        cmd = tc.Command()
        with mock.patch.object(tc, "get_commands", return_value={"seed_products": "products"}), \
             mock.patch.object(tc, "load_command_class", return_value=_Klass()), \
             mock.patch.object(tc.Command, "get_tenant_from_options_or_interactive", return_value=t), \
             mock.patch("tenants.context.connections", {"shard_9": _FakeConn()}):
            cmd.run_from_argv(["manage.py", "tenant_command", "seed_products", "--schema=beta"])

        self.assertEqual(seen["db"], "shard_9")
        self.assertIsNone(bound_alias())
