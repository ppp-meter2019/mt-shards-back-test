"""Shard-aware context managers (tenants.context) — reentrancy / restore semantics — plus
TenantShardRoutingMiddleware, which is now a thin CONSUMER of them (same two axes, so the
coverage belongs together). DB-free: `connections` is mocked with fake connection objects
that record the schema switch.

NB: current_db's unset sentinel is None (no routing context); a with-block restores to None
at the top level. An explicit alias (incl. schema_context("public") → "default") is a real set."""
import types
from unittest import mock

from django.db.utils import ConnectionDoesNotExist
from django.test import SimpleTestCase

import tenants.middleware as middleware
from tenants.context import (
    active_alias, bound_alias, schema_context, tenant_context, use_alias,
)


class _FakeConn:
    """Records the django_tenants schema-switch surface _switch() drives."""
    def __init__(self, tenant=None):
        self.tenant = tenant
        self.schema = None

    def set_tenant(self, t):
        self.tenant = t

    def set_schema(self, name):
        self.schema = name

    def set_schema_to_public(self):
        self.tenant = None
        self.schema = "public"


def _tenant(alias):
    return types.SimpleNamespace(shard=types.SimpleNamespace(alias=alias))


class TenantContextTests(SimpleTestCase):
    def _conns(self, *aliases):
        return {a: _FakeConn() for a in aliases}

    def test_sets_and_restores_current_db(self):
        conns = self._conns("shard_a")
        with mock.patch("tenants.context.connections", conns):
            self.assertIsNone(bound_alias())                 # unset at top level
            with tenant_context(_tenant("shard_a")):
                self.assertEqual(bound_alias(), "shard_a")   # axis 1 set
                self.assertIsNotNone(conns["shard_a"].tenant)   # axis 2 set
            self.assertIsNone(bound_alias())                 # restored to unset
            self.assertIsNone(conns["shard_a"].tenant)          # restored (prev was None)

    def test_nested_different_shards_restore_lifo(self):
        conns = self._conns("shard_a", "shard_b")
        with mock.patch("tenants.context.connections", conns):
            with tenant_context(_tenant("shard_a")):
                with tenant_context(_tenant("shard_b")):
                    self.assertEqual(bound_alias(), "shard_b")
                self.assertEqual(bound_alias(), "shard_a")   # inner exit restored to A
            self.assertIsNone(bound_alias())

    def test_nested_SAME_shard_restores_prev_tenant(self):
        # The reentrancy clobber the class version was vulnerable to: reused state would make
        # the inner exit restore the WRONG previous tenant. Generator frames keep it correct.
        conns = self._conns("shard_a")
        t1, t2 = _tenant("shard_a"), _tenant("shard_a")
        with mock.patch("tenants.context.connections", conns):
            with tenant_context(t1):
                self.assertIs(conns["shard_a"].tenant, t1)
                with tenant_context(t2):
                    self.assertIs(conns["shard_a"].tenant, t2)
                self.assertIs(conns["shard_a"].tenant, t1)      # ← restored to t1, not clobbered
            self.assertIsNone(conns["shard_a"].tenant)

    def test_restores_on_exception(self):
        conns = self._conns("shard_a")
        with mock.patch("tenants.context.connections", conns):
            with self.assertRaises(ValueError):
                with tenant_context(_tenant("shard_a")):
                    raise ValueError("boom")
            self.assertIsNone(bound_alias())                 # finally ran despite the raise
            self.assertIsNone(conns["shard_a"].tenant)

    def test_explicit_database_overrides_shard(self):
        conns = self._conns("override")
        with mock.patch("tenants.context.connections", conns):
            with tenant_context(_tenant("shard_a"), database="override"):
                self.assertEqual(bound_alias(), "override")


class SchemaContextTests(SimpleTestCase):
    def test_public_short_circuits_to_default(self):
        conns = {"default": _FakeConn()}
        with mock.patch("tenants.context.connections", conns):
            with schema_context("public"):
                self.assertEqual(bound_alias(), "default")   # explicit "default" (not the None sentinel)
                self.assertEqual(conns["default"].schema, "public")
            self.assertIsNone(bound_alias())                 # restored to unset

    def test_explicit_database_sets_schema(self):
        conns = {"shard_x": _FakeConn()}
        with mock.patch("tenants.context.connections", conns):
            with schema_context("acme", database="shard_x"):
                self.assertEqual(bound_alias(), "shard_x")
                self.assertEqual(conns["shard_x"].schema, "acme")

    def test_unknown_schema_raises_with_hint(self):
        from tenants.models import Tenant
        with mock.patch("tenants.models.Tenant.objects") as objs:
            objs.select_related.return_value.get.side_effect = Tenant.DoesNotExist
            with self.assertRaises(Tenant.DoesNotExist):
                with schema_context("ghost"):
                    pass


class UseAliasTests(SimpleTestCase):
    def test_sets_and_restores(self):
        self.assertIsNone(bound_alias())
        with use_alias("shard_z"):
            self.assertEqual(bound_alias(), "shard_z")
        self.assertIsNone(bound_alias())


class ActiveAliasTests(SimpleTestCase):
    def test_none_coalesces_to_default(self):
        self.assertIsNone(bound_alias())
        self.assertEqual(active_alias(), "default")             # unset → default (for readers)
        with use_alias("shard_q"):
            self.assertEqual(active_alias(), "shard_q")


class RoutingMiddlewareTests(SimpleTestCase):
    """TenantShardRoutingMiddleware wires BOTH axes through tenants.context and unwinds them.

    It had no behavioural test at all while it hand-rolled the switch — only its NAME was
    checked, by the middleware-order invariant in test_settings_invariants.
    """

    class _Conns(dict):
        """connections[...] that raises like Django's, so the unknown-alias path is faithful."""

        def __getitem__(self, key):
            if key not in self:
                raise ConnectionDoesNotExist(key)
            return dict.__getitem__(self, key)

    def _run(self, tenant, conns, get_response):
        mw = middleware.TenantShardRoutingMiddleware(get_response)
        request = types.SimpleNamespace(tenant=tenant)
        with mock.patch.object(middleware, "connections", conns), \
                mock.patch("tenants.context.connections", conns):
            return mw(request)

    def test_shard_request_wires_both_axes_and_unwinds(self):
        conns = self._Conns(shard_a=_FakeConn())
        seen = {}

        def view(request):
            seen["axis1"] = bound_alias()
            seen["axis2"] = conns["shard_a"].tenant
            return "resp"

        self.assertEqual(self._run(_tenant("shard_a"), conns, view), "resp")
        self.assertEqual(seen["axis1"], "shard_a")           # router axis live in the view
        self.assertIsNotNone(seen["axis2"])                  # schema axis live in the view
        self.assertIsNone(bound_alias())                  # router axis unwound
        self.assertEqual(conns["shard_a"].schema, "public")  # schema axis unwound

    def test_axes_unwind_when_the_view_raises(self):
        conns = self._Conns(shard_a=_FakeConn())

        def boom(request):
            raise RuntimeError("view exploded")

        with self.assertRaises(RuntimeError):
            self._run(_tenant("shard_a"), conns, boom)
        self.assertIsNone(bound_alias())
        self.assertEqual(conns["shard_a"].schema, "public")

    def test_public_tenant_pins_router_axis_only(self):
        """shard.alias == "default": ShardAwareTenantMiddleware already owns the schema there
        (and re-resets it at the START of each request), so axis 2 must be left alone."""
        conns = self._Conns(default=_FakeConn())
        seen = {}

        def view(request):
            seen["axis1"] = bound_alias()
            return "resp"

        self._run(_tenant("default"), conns, view)
        self.assertEqual(seen["axis1"], "default")           # pinned EXPLICITLY, not inherited
        self.assertIsNone(conns["default"].schema)           # axis 2 untouched
        self.assertIsNone(bound_alias())

    def test_unknown_alias_does_not_leak_current_db(self):
        """Regression: the alias is resolved BEFORE either axis is touched. The previous
        hand-rolled version set current_db first, so a Shard row whose alias had been dropped
        from settings.DATABASES left the router axis pinned for the life of the thread."""
        conns = self._Conns(shard_a=_FakeConn())             # no "ghost" entry
        with self.assertRaises(ConnectionDoesNotExist):
            self._run(_tenant("ghost"), conns, lambda r: "resp")
        self.assertIsNone(bound_alias())

    def test_exit_forces_public_over_a_foreign_tenant(self):
        """The exit reset is deliberately NOT delegated to tenant_context, which restores the
        connection's PREVIOUS tenant. If a foreign tenant were somehow left on this shard
        connection, restoring it would hand the next caller another tenant's schema; forcing
        `public` (which on a shard holds only the postgis extension) fails loudly instead."""
        foreign = types.SimpleNamespace(schema_name="beta")
        conns = self._Conns(shard_a=_FakeConn(tenant=foreign))
        self._run(_tenant("shard_a"), conns, lambda r: "resp")
        self.assertEqual(conns["shard_a"].schema, "public")
        self.assertIsNot(conns["shard_a"].tenant, foreign)
