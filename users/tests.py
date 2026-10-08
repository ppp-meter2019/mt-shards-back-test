"""users mode-safety — _on_tenant (MT vs standalone), the standalone URLconf smoke and the
Celery app-type gate. DB-free (mocks + override_settings).

The session-guard tests moved to tenants/tests/test_auth_binding.py with the middleware
itself (tenants/auth/session.py): it is MT-only, and `tenants` is not installed here."""
from unittest import mock

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve

from users.permissions import _on_tenant


class OnTenantModeTests(SimpleTestCase):
    """users.permissions._on_tenant — the gate EVERY business viewset uses.

    Full contract, forced per-branch with override_settings so it passes in BOTH
    CI modes (the MT suite and the real USE_MULTITENANT=0 standalone suite)."""

    def setUp(self):
        self.rf = RequestFactory()

    @override_settings(USE_MULTITENANT=False)
    def test_true_in_standalone(self):
        # Standalone has no public/tenant split — the single DB IS the tenant, so
        # the gate is always open. MUST NOT read connection.schema_name here (it
        # does not exist on the plain PostGIS backend). Regression guard for §5.
        self.assertTrue(_on_tenant(self.rf.get("/api/cars/")))

    @override_settings(USE_MULTITENANT=True)
    @mock.patch("users.permissions.connection")
    def test_true_on_a_tenant_schema(self, conn):
        conn.schema_name = "alpha"
        self.assertTrue(_on_tenant(self.rf.get("/api/cars/")))

    @override_settings(USE_MULTITENANT=True)
    @mock.patch("users.permissions.connection")
    def test_false_on_public(self, conn):
        conn.schema_name = "public"
        self.assertFalse(_on_tenant(self.rf.get("/api/cars/")))


@override_settings(ROOT_URLCONF="tenants_back.urls_standalone", USE_MULTITENANT=False)
class StandaloneUrlconfTests(SimpleTestCase):
    """Import-contract for the standalone URLconf: resolving forces
    urls_standalone + all business viewsets to import with tenants NOT active.
    The strongest signal is the real standalone CI run (users suite, tenants not
    even registered); in the MT suite this is a lighter proxy."""

    def test_health_wired(self):
        self.assertEqual(resolve("/api/health/").func.__name__, "health")

    def test_business_router_wired(self):
        self.assertIsNotNone(resolve("/api/cars/").func)

class CeleryModeGateTests(SimpleTestCase):
    """Boot smoke for the celery.py mode gate — the most MT-branched wiring the rest of the
    standalone suite does not exercise. The app singleton is built at IMPORT from the real
    USE_MULTITENANT, so its type proves which branch actually booted:
      standalone -> plain celery.Celery (no tenants.celery / django_tenants pulled in to boot);
      MT         -> the shard-aware tenants.celery.CeleryApp subclass."""

    def test_app_type_matches_mode(self):
        from django.conf import settings
        from celery import Celery
        from tenants_back.celery import app
        self.assertIsInstance(app, Celery)
        if settings.USE_MULTITENANT:
            self.assertIsNot(type(app), Celery)                        # a subclass
            self.assertTrue(type(app).__module__.startswith("tenants"))
        else:
            self.assertIs(type(app), Celery)                           # plain — standalone branch
