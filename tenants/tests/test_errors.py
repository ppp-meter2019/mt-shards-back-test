"""tenants.errors: content negotiation, no-store, and the missing-template fallback
(the error path must never itself raise)."""
from django.test import RequestFactory, SimpleTestCase

from tenants.errors import error_response, wants_json


class ErrorResponseHelperTests(SimpleTestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def test_wants_json_by_path_or_accept(self):
        self.assertTrue(wants_json(self.rf.get("/api/v1/x")))
        self.assertTrue(wants_json(self.rf.get("/", HTTP_ACCEPT="application/json")))
        self.assertFalse(wants_json(self.rf.get("/", HTTP_ACCEPT="text/html")))

    def test_no_store_and_retry_after(self):
        r = error_response(self.rf.get("/api/v1/x"), status=503, code="c", detail="d",
                           template="tenants/errors/not_ready.html", retry_after=300)
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(r["Retry-After"], "300")

    def test_missing_template_falls_back_without_raising(self):
        r = error_response(self.rf.get("/", HTTP_ACCEPT="text/html"), status=500, code="c",
                           detail="Boom", template="tenants/errors/__does_not_exist__.html")
        self.assertEqual(r.status_code, 500)
        self.assertIn(b"Boom", r.content)          # fallback rendered
        self.assertEqual(r["Cache-Control"], "no-store")

    def test_extra_merged_into_json_body(self):
        import json
        r = error_response(self.rf.get("/api/v1/x"), status=503, code="tenant_not_ready",
                           detail="d", template="x", extra={"status": "new"})
        self.assertEqual(json.loads(r.content)["status"], "new")


class ErrorTemplateRenderTests(SimpleTestCase):
    """Each branded page extends _base.html and renders (catches a broken base/blocks)."""

    def test_all_pages_render_with_base(self):
        from django.template.loader import render_to_string
        cases = {
            "tenants/errors/deactivated.html": "Account unavailable",
            "tenants/errors/not_ready.html": "Temporarily unavailable",
            "tenants/errors/not_found.html": "Page not found",
            "tenants/errors/database_error.html": "Something went wrong",
        }
        for template, heading in cases.items():
            html = render_to_string(template)
            self.assertIn(heading, html)
            self.assertIn(".card", html)                 # base CSS applied
            self.assertIn("routegenie.com", html)