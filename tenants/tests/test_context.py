"""Shard-aware context managers (tenants.context) — reentrancy / restore semantics.
DB-free: `connections` is mocked with fake connection objects that record the schema switch.

NB: current_db's unset sentinel is None (no routing context); a with-block restores to None
at the top level. An explicit alias (incl. schema_context("public") → "default") is a real set."""
import types
from unittest import mock

from django.test import SimpleTestCase

from tenants.context import active_alias, current_db, schema_context, tenant_context, use_alias


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
            self.assertIsNone(current_db.get())                 # unset at top level
            with tenant_context(_tenant("shard_a")):
                self.assertEqual(current_db.get(), "shard_a")   # axis 1 set
                self.assertIsNotNone(conns["shard_a"].tenant)   # axis 2 set
            self.assertIsNone(current_db.get())                 # restored to unset
            self.assertIsNone(conns["shard_a"].tenant)          # restored (prev was None)

    def test_nested_different_shards_restore_lifo(self):
        conns = self._conns("shard_a", "shard_b")
        with mock.patch("tenants.context.connections", conns):
            with tenant_context(_tenant("shard_a")):
                with tenant_context(_tenant("shard_b")):
                    self.assertEqual(current_db.get(), "shard_b")
                self.assertEqual(current_db.get(), "shard_a")   # inner exit restored to A
            self.assertIsNone(current_db.get())

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
            self.assertIsNone(current_db.get())                 # finally ran despite the raise
            self.assertIsNone(conns["shard_a"].tenant)

    def test_explicit_database_overrides_shard(self):
        conns = self._conns("override")
        with mock.patch("tenants.context.connections", conns):
            with tenant_context(_tenant("shard_a"), database="override"):
                self.assertEqual(current_db.get(), "override")


class SchemaContextTests(SimpleTestCase):
    def test_public_short_circuits_to_default(self):
        conns = {"default": _FakeConn()}
        with mock.patch("tenants.context.connections", conns):
            with schema_context("public"):
                self.assertEqual(current_db.get(), "default")   # explicit "default" (not the None sentinel)
                self.assertEqual(conns["default"].schema, "public")
            self.assertIsNone(current_db.get())                 # restored to unset

    def test_explicit_database_sets_schema(self):
        conns = {"shard_x": _FakeConn()}
        with mock.patch("tenants.context.connections", conns):
            with schema_context("acme", database="shard_x"):
                self.assertEqual(current_db.get(), "shard_x")
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
        self.assertIsNone(current_db.get())
        with use_alias("shard_z"):
            self.assertEqual(current_db.get(), "shard_z")
        self.assertIsNone(current_db.get())


class ActiveAliasTests(SimpleTestCase):
    def test_none_coalesces_to_default(self):
        self.assertIsNone(current_db.get())
        self.assertEqual(active_alias(), "default")             # unset → default (for readers)
        with use_alias("shard_q"):
            self.assertEqual(active_alias(), "shard_q")
