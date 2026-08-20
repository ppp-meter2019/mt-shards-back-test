"""Status gate: only ACTIVE business tenants are served; PUBLIC is exempt;
FAILED keeps its own /admin/ reachable. DB-free (super().process_request mocked)."""
from types import SimpleNamespace
from unittest import mock

from django.db import OperationalError
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase

import tenants.middleware as mw
from tenants.models import Tenant
from tenants.resolver import resolve_cache

S = Tenant.Status


class GateStatusMatrixTests(SimpleTestCase):
    def setUp(self):
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)

    def _gate(self, status, path="/api/v1/x", schema="alpha"):
        req = SimpleNamespace(path=path, META={},
                              tenant=SimpleNamespace(status=status, schema_name=schema))
        with mock.patch.object(mw.TenantMainMiddleware, "process_request", return_value=None):
            resp = self.mw.process_request(req)
        return None if resp is None else resp.status_code

    def test_active_passes(self):
        self.assertIsNone(self._gate(S.ACTIVE, "/api/v1/x"))
        self.assertIsNone(self._gate(S.ACTIVE, "/admin/"))

    def test_deactivated_403_whole_host(self):
        self.assertEqual(self._gate(S.DEACTIVATED, "/api/v1/x"), 403)
        self.assertEqual(self._gate(S.DEACTIVATED, "/admin/"), 403)

    def test_new_and_pending_503_including_admin(self):
        for st in (S.NEW, S.PENDING):
            self.assertEqual(self._gate(st, "/api/v1/x"), 503)
            self.assertEqual(self._gate(st, "/admin/"), 503)

    def test_failed_api_503_but_admin_exempt(self):
        self.assertEqual(self._gate(S.FAILED, "/api/v1/x"), 503)
        self.assertIsNone(self._gate(S.FAILED, "/admin/"))
        self.assertIsNone(self._gate(S.FAILED, "/admin"))            # no trailing slash
        self.assertIsNone(self._gate(S.FAILED, "/admin/login/"))
        self.assertEqual(self._gate(S.FAILED, "/administrators"), 503)  # not a false prefix match
        self.assertEqual(self._gate(S.FAILED, "/other"), 503)

    def test_public_tenant_exempt_regardless_of_status(self):
        for st in (S.NEW, S.PENDING, S.FAILED, S.DEACTIVATED):
            self.assertIsNone(self._gate(st, "/admin/", schema="public"))

    def test_health_path_short_circuits_before_gate(self):
        req = SimpleNamespace(path="/api/health/", tenant=None)
        resp = self.mw.process_request(req)   # answered before super()/gate
        self.assertEqual(resp.status_code, 200)


class ErrorResponseNegotiationTests(SimpleTestCase):
    """JSON for API/JSON clients, branded HTML for browsers; Retry-After on 503."""

    def setUp(self):
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)
        self.rf = RequestFactory()

    def _run(self, status, path="/x/", accept=""):
        req = self.rf.get(path, HTTP_ACCEPT=accept)
        req.tenant = SimpleNamespace(status=status, schema_name="alpha")
        with mock.patch.object(mw.TenantMainMiddleware, "process_request", return_value=None):
            return self.mw.process_request(req)

    def test_deactivated_json_for_api_path(self):
        r = self._run(S.DEACTIVATED, "/api/v1/x")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r["Content-Type"], "application/json")

    def test_deactivated_html_for_browser(self):
        r = self._run(S.DEACTIVATED, "/", accept="text/html")
        self.assertEqual(r.status_code, 403)
        self.assertIn("text/html", r["Content-Type"])

    def test_not_ready_json_has_retry_after(self):
        r = self._run(S.NEW, "/api/v1/x")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r["Content-Type"], "application/json")
        self.assertEqual(r["Retry-After"], "300")

    def test_not_ready_html_for_browser_has_retry_after(self):
        r = self._run(S.NEW, "/", accept="text/html")
        self.assertEqual(r.status_code, 503)
        self.assertIn("text/html", r["Content-Type"])
        self.assertEqual(r["Retry-After"], "300")

    def test_accept_json_forces_json_on_non_api_path(self):
        r = self._run(S.NEW, "/", accept="application/json")
        self.assertEqual(r["Content-Type"], "application/json")


class TenantResolutionErrorTests(SimpleTestCase):
    """Unknown host -> branded 404, DB unreachable -> branded 500 (negotiated)."""

    def setUp(self):
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)
        self.rf = RequestFactory()

    def _run(self, exc, path="/x/", accept=""):
        req = self.rf.get(path, HTTP_ACCEPT=accept)
        with mock.patch.object(mw.TenantMainMiddleware, "process_request", side_effect=exc):
            return self.mw.process_request(req)

    def test_unknown_host_404_json(self):
        r = self._run(Http404("no tenant"), "/api/v1/x")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r["Content-Type"], "application/json")

    def test_unknown_host_404_html(self):
        r = self._run(Http404("no tenant"), "/", accept="text/html")
        self.assertEqual(r.status_code, 404)
        self.assertIn("text/html", r["Content-Type"])

    def test_db_unreachable_500_json(self):
        r = self._run(OperationalError("db down"), "/api/v1/x")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r["Content-Type"], "application/json")

    def test_db_unreachable_500_html(self):
        r = self._run(OperationalError("db down"), "/", accept="text/html")
        self.assertEqual(r.status_code, 500)
        self.assertIn("text/html", r["Content-Type"])

    def test_get_tenant_reraises_operational_error(self):
        m = mw.ShardAwareTenantMiddleware(lambda r: None)

        class DNE(Exception):
            pass

        class FakeDomain:
            DoesNotExist = DNE

        # enabled is True from settings; get_snapshot raising OperationalError must be
        # re-raised (surface for the 500 handler), NOT swallowed / retried against the DB.
        with mock.patch.object(resolve_cache, "get_snapshot", side_effect=OperationalError("down")):
            with self.assertRaises(OperationalError):
                m.get_tenant(FakeDomain, "h")
