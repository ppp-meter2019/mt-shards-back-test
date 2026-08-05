"""SchemaBoundSessionMiddleware — session-side tenant binding. DB-free
(RequestFactory + fake session + mocked connection/logout)."""
from unittest import mock

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.test import RequestFactory, SimpleTestCase

from users.middleware import SchemaBoundSessionMiddleware


class _FakeSession(dict):
    def flush(self):
        self.clear()


class SchemaBoundSessionGuardTests(SimpleTestCase):
    def setUp(self):
        self.get_response = mock.Mock(return_value="OK")
        self.mw = SchemaBoundSessionMiddleware(self.get_response)
        self.rf = RequestFactory()

    def _req(self, path="/admin/", cookie=True, session=None):
        req = self.rf.get(path)
        if cookie:
            req.COOKIES[settings.SESSION_COOKIE_NAME] = "x"
        req.session = session if session is not None else _FakeSession()
        return req

    def _run(self, req, schema="alpha"):
        with mock.patch("users.middleware.connection") as conn:
            conn.schema_name = schema
            return self.mw(req)

    def test_no_cookie_passes_without_touching_session(self):
        touched = {"n": 0}

        class Sess(_FakeSession):
            def get(self, *a, **k):
                touched["n"] += 1
                return super().get(*a, **k)

        s = Sess({SESSION_KEY: 1, "schema": "beta"})   # would mismatch if inspected
        self.assertEqual(self._run(self._req(cookie=False, session=s)), "OK")
        self.assertEqual(touched["n"], 0)              # session never inspected

    def test_api_path_exempt(self):
        s = _FakeSession({SESSION_KEY: 1, "schema": "beta"})
        self.assertEqual(self._run(self._req(path="/api/v1/x", session=s)), "OK")

    def test_anonymous_session_passes(self):
        self.assertEqual(self._run(self._req(session=_FakeSession())), "OK")

    def test_same_schema_passes(self):
        s = _FakeSession({SESSION_KEY: 1, "schema": "alpha"})
        self.assertEqual(self._run(self._req(session=s)), "OK")

    def test_cross_tenant_rejected(self):
        s = _FakeSession({SESSION_KEY: 1, "schema": "beta"})
        with mock.patch("users.middleware.logout") as logout:
            resp = self._run(self._req(session=s))
        self.assertEqual(resp.status_code, 302)        # redirect to login
        logout.assert_called_once()

    def test_missing_schema_is_fail_closed(self):
        s = _FakeSession({SESSION_KEY: 1})             # authenticated, no schema stamp
        with mock.patch("users.middleware.logout") as logout:
            resp = self._run(self._req(session=s))
        self.assertEqual(resp.status_code, 302)
        logout.assert_called_once()