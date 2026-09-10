"""Physical-state probes degrade per shard (DB-free): a down/unreachable shard must
not propagate — its tenants just fall out of the result — so the tenants console
stays up. `connections` is mocked, so no Postgres is needed."""
from types import SimpleNamespace
from unittest import mock

from django.db.utils import OperationalError
from django.test import SimpleTestCase

from tenants.views import TenantViewSet


def _tenant(schema, alias):
    return SimpleNamespace(schema_name=schema, shard=SimpleNamespace(alias=alias))


def _dead_connections():
    """A `connections`-like mock whose cursor() raises OperationalError (shard down)."""
    conns = mock.MagicMock()
    conns.__getitem__.return_value.cursor.side_effect = OperationalError("shard down")
    return conns


class ProbeDegradeTests(SimpleTestCase):
    def test_existing_schemas_degrades_on_dead_shard(self):
        qs = [_tenant("alpha", "shard_x")]
        with mock.patch("tenants.views.connections", _dead_connections()):
            result = TenantViewSet._existing_schemas_for(qs)   # must not raise
        self.assertEqual(result, set())

    def test_last_migrations_degrades_on_dead_shard(self):
        qs = [_tenant("alpha", "shard_x")]
        with mock.patch("tenants.views.connections", _dead_connections()):
            result = TenantViewSet._last_migrations_for(qs)    # must not raise
        self.assertEqual(result, {})

    def test_programming_error_is_not_swallowed(self):
        # A non-DB error (e.g. a bug) must propagate, not degrade to empty.
        conns = mock.MagicMock()
        conns.__getitem__.return_value.cursor.side_effect = KeyError("bug")
        with mock.patch("tenants.views.connections", conns):
            with self.assertRaises(KeyError):
                TenantViewSet._existing_schemas_for([_tenant("alpha", "shard_x")])


def _scripted_connections(script):
    """A `connections`-like mock whose cursor().fetchall() returns each element of `script`
    in turn, and which records every executed statement in `.statements`."""
    statements = []

    class _Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): statements.append((sql, params))
        def fetchall(self): return script.pop(0)

    conns = mock.MagicMock()
    conns.__getitem__.return_value.cursor.side_effect = lambda: _Cursor()
    conns.statements = statements
    return conns


class AdminsProbeTests(SimpleTestCase):
    """_admins_for batches per SHARD. Serializing this field per tenant cost a
    `SET search_path` plus a `SELECT` for every row (2N round-trips for a list); the probe
    replaces that with one pair of queries per shard, the same shape the sibling probes use.
    """

    def test_degrades_on_dead_shard(self):
        qs = [_tenant("alpha", "shard_x")]
        with mock.patch("tenants.views.connections", _dead_connections()):
            self.assertEqual(TenantViewSet._admins_for(qs), {})   # must not raise

    def test_programming_error_is_not_swallowed(self):
        conns = mock.MagicMock()
        conns.__getitem__.return_value.cursor.side_effect = KeyError("bug")
        with mock.patch("tenants.views.connections", conns):
            with self.assertRaises(KeyError):
                TenantViewSet._admins_for([_tenant("alpha", "shard_x")])

    def test_one_pair_of_queries_per_shard_not_per_tenant(self):
        """Four tenants on one shard must still cost exactly two statements."""
        script = [
            [("alpha",), ("beta",), ("gamma",), ("delta",)],          # schemas with the table
            [("alpha", 1, "root", True), ("beta", 1, "boss", True)],  # the UNION result
        ]
        conns = _scripted_connections(script)
        qs = [_tenant(s, "shard_x") for s in ("alpha", "beta", "gamma", "delta")]
        with mock.patch("tenants.views.connections", conns):
            result = TenantViewSet._admins_for(qs)
        self.assertEqual(len(conns.statements), 2)
        self.assertEqual(result, {
            ("shard_x", "alpha"): [{"id": 1, "username": "root", "is_active": True}],
            ("shard_x", "beta"): [{"id": 1, "username": "boss", "is_active": True}],
        })

    def test_union_has_one_branch_per_schema_that_has_the_table(self):
        """gamma is absent from the first result, so it must not appear in the UNION."""
        script = [[("alpha",), ("beta",)], []]
        conns = _scripted_connections(script)
        qs = [_tenant(s, "shard_x") for s in ("alpha", "beta", "gamma")]
        with mock.patch("tenants.views.connections", conns):
            TenantViewSet._admins_for(qs)
        union = conns.statements[1][0]
        self.assertEqual(union.count("UNION ALL"), 1)          # 2 branches
        self.assertIn('"alpha".users_user', union)
        self.assertIn('"beta".users_user', union)
        self.assertNotIn("gamma", union)

    def test_role_is_passed_as_a_parameter_per_branch(self):
        script = [[("alpha",), ("beta",)], []]
        conns = _scripted_connections(script)
        with mock.patch("tenants.views.connections", conns):
            TenantViewSet._admins_for([_tenant(s, "shard_x") for s in ("alpha", "beta")])
        _sql, params = conns.statements[1]
        self.assertEqual(params, ["company_admin", "company_admin"])   # one per branch

    def test_schema_identifiers_are_quoted_and_filtered(self):
        """Names come from the DB, but they are interpolated as SQL identifiers — so the
        safety floor filters them and quote_schema quotes what survives."""
        script = [[("alpha",)], []]
        conns = _scripted_connections(script)
        qs = [_tenant("alpha", "shard_x"), _tenant('a"b', "shard_x")]
        with mock.patch("tenants.views.connections", conns):
            TenantViewSet._admins_for(qs)
        first_params = conns.statements[0][1]
        self.assertEqual(first_params[1], ["alpha"])           # 'a"b' never reaches SQL
        self.assertIn('"alpha".users_user', conns.statements[1][0])

    def test_shards_are_probed_independently(self):
        script = [[("alpha",)], [("alpha", 1, "root", True)],
                  [("beta",)], [("beta", 2, "boss", False)]]
        conns = _scripted_connections(script)
        qs = [_tenant("alpha", "shard_x"), _tenant("beta", "shard_y")]
        with mock.patch("tenants.views.connections", conns):
            result = TenantViewSet._admins_for(qs)
        self.assertEqual(len(conns.statements), 4)             # 2 per shard
        self.assertEqual(set(result), {("shard_x", "alpha"), ("shard_y", "beta")})


class AdminsSerializerFieldTests(SimpleTestCase):
    """get_admins must be a pure context read. It used to run an ORM query inside
    tenant_context(obj) — the N in the 2N — so a regression here silently restores it."""

    def _serializer(self, context):
        from tenants.serializers import TenantSerializer
        return TenantSerializer(context=context)

    def test_reads_the_precomputed_table(self):
        admins = {("shard_x", "alpha"): [{"id": 1, "username": "root", "is_active": True}]}
        got = self._serializer({"admins": admins}).get_admins(_tenant("alpha", "shard_x"))
        self.assertEqual(got, [{"id": 1, "username": "root", "is_active": True}])

    def test_missing_tenant_is_empty_not_an_error(self):
        got = self._serializer({"admins": {}}).get_admins(_tenant("alpha", "shard_x"))
        self.assertEqual(got, [])

    def test_absent_context_is_empty(self):
        """Used outside the viewset (create/update, or a bare serializer) — same contract
        as schema_exists and last_migration."""
        got = self._serializer({}).get_admins(_tenant("alpha", "shard_x"))
        self.assertEqual(got, [])

    def test_issues_no_queries_and_never_switches_schema(self):
        """Pinned two ways: a `connections` that explodes on use, and tenant_context
        asserted absent from the module — the per-tenant version needed both."""
        conns = mock.MagicMock()
        conns.__getitem__.side_effect = AssertionError("get_admins must not touch the DB")
        with mock.patch("tenants.views.connections", conns):
            self.assertEqual(
                self._serializer({"admins": {}}).get_admins(_tenant("alpha", "shard_x")), [])
        import tenants.serializers as ser
        self.assertFalse(hasattr(ser, "tenant_context"),
                         "serializers must not import tenant_context any more")
