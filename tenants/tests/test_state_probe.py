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
