"""BaseDomainsView payload (DB-free). Permission gating (IsTenantAdminOnPublic) is
covered by the DB-backed harness (db_integration.BaseDomainsEndpointDBTests)."""
from django.test import SimpleTestCase, override_settings

from tenants.views import BaseDomainsView


class BaseDomainsPayloadTests(SimpleTestCase):
    @override_settings(TENANT_BASE_DOMAINS=("routegenie.com", "isi-technology.com"))
    def test_returns_configured_bases(self):
        # Call get() directly (bypasses permissions) — this asserts the wiring:
        # the endpoint reflects settings.TENANT_BASE_DOMAINS, in order, as a list.
        resp = BaseDomainsView().get(None)
        self.assertEqual(resp.data, {"base_domains": ["routegenie.com", "isi-technology.com"]})

    @override_settings(TENANT_BASE_DOMAINS=())
    def test_empty_when_unset(self):
        self.assertEqual(BaseDomainsView().get(None).data, {"base_domains": []})
