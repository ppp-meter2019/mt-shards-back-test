"""current_schema_name (tenants.celery.compat) — what gets stamped into _schema_name.

Two separate contracts, and the tests keep them apart:
  * NO ROUTING CONTEXT  -> public, decided by bound_alias() being None, never by reading a
    connection. Legitimate and reached on the request path (HostRegistry.trigger_warm).
  * BOUND context       -> that connection's schema; an unexpected error there must NOT be
    swallowed into public (that would mis-dispatch a tenant task to default.public).
Every test therefore states explicitly whether an alias is bound."""
import types
from unittest import mock

from django.test import SimpleTestCase


class CurrentSchemaNameTests(SimpleTestCase):
    def test_returns_the_bound_shard_schema(self) -> None:
        """A bound tenant context stamps that shard's schema — not `default`'s."""
        from tenants.celery import compat
        with mock.patch.object(compat, "connections",
                               {"tenant_1": types.SimpleNamespace(schema_name="acme"),
                                "default": types.SimpleNamespace(schema_name="public")}), \
             mock.patch.object(compat, "bound_alias", lambda: "tenant_1"):
            self.assertEqual(compat.current_schema_name(), "acme")

    def test_empty_schema_on_a_bound_connection_falls_to_public(self) -> None:
        """Distinct from "no context": here an alias IS bound, the connection just has
        nothing set on it."""
        from tenants.celery import compat
        with mock.patch.object(compat, "connections",
                               {"default": types.SimpleNamespace(schema_name="")}), \
             mock.patch.object(compat, "bound_alias", lambda: "default"):
            self.assertEqual(compat.current_schema_name(), compat.get_public_schema_name())

    def test_unbound_axis_stamps_public_not_the_stale_default(self) -> None:
        """trigger_warm() enqueues with no bound axis, from inside
        ShardAwareTenantMiddleware. The stamp must be public BY CONSTRUCTION, not because
        connections['default'] happens to be public at that moment: upstream resets it on the
        first line of process_request, but after set_tenant() the same read would stamp the
        tenant being served onto an unrelated task, and TenantTask.__call__ would enter that
        tenant in full — no error, wrong tenant."""
        from tenants.celery import compat
        with mock.patch.object(compat, "connections",
                               {"default": types.SimpleNamespace(schema_name="acme")}), \
             mock.patch.object(compat, "bound_alias", lambda: None):
            self.assertEqual(compat.current_schema_name(), compat.get_public_schema_name())

    def test_unexpected_error_raises_not_public(self) -> None:
        """Only reachable with an alias BOUND — an unbound axis never touches a connection."""
        from tenants.celery import compat

        class _Boom:
            @property
            def schema_name(self) -> None:
                raise RuntimeError("bad connection state")

        with mock.patch.object(compat, "connections", {"default": _Boom()}), \
             mock.patch.object(compat, "bound_alias", lambda: "default"):
            with self.assertRaises(RuntimeError):        # fail-loud, NOT silent public
                compat.current_schema_name()
