"""Status gate: only ACTIVE business tenants are served; PUBLIC is exempt;
FAILED keeps its own /admin/ reachable. DB-free (super().process_request mocked)."""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

import tenants.middleware as mw
from tenants.models import Tenant

S = Tenant.Status


class GateStatusMatrixTests(SimpleTestCase):
    def setUp(self):
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)

    def _gate(self, status, path="/api/v1/x", schema="alpha"):
        req = SimpleNamespace(path=path, tenant=SimpleNamespace(status=status, schema_name=schema))
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
