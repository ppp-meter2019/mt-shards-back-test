"""current_schema_name (tenants.celery.compat) — fail-loud on unexpected error (A2).
The legit 'no schema' case → public via `or`; an unexpected error must NOT silently stamp
public (that would mis-dispatch a tenant task to default.public)."""
import types
from unittest import mock

from django.test import SimpleTestCase


class CurrentSchemaNameTests(SimpleTestCase):
    def test_returns_connection_schema(self) -> None:
        from tenants.celery import compat
        with mock.patch.object(compat, "connections",
                               {"default": types.SimpleNamespace(schema_name="acme")}):
            self.assertEqual(compat.current_schema_name(), "acme")

    def test_empty_schema_falls_to_public(self) -> None:
        from tenants.celery import compat
        with mock.patch.object(compat, "connections",
                               {"default": types.SimpleNamespace(schema_name="")}):
            self.assertEqual(compat.current_schema_name(), compat.get_public_schema_name())

    def test_unexpected_error_raises_not_public(self) -> None:
        from tenants.celery import compat

        class _Boom:
            @property
            def schema_name(self) -> None:
                raise RuntimeError("bad connection state")

        with mock.patch.object(compat, "connections", {"default": _Boom()}):
            with self.assertRaises(RuntimeError):        # fail-loud, NOT silent public
                compat.current_schema_name()
