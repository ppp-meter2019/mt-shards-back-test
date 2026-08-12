"""DB-backed integration tests for the reserved-host feature.

Requires Postgres and runs the real migrations (incl. the 0004 seed). This module is
DELIBERATELY named without a ``test_`` prefix so the default, DB-free discovery
(``manage.py test tenants.tests``) does NOT collect it — otherwise every run would
need a database. Run it explicitly against a real DB:

    python manage.py test tenants.tests.db_integration

It exercises what the DB-free suite must mock: the seed rows, the reserved-host
queries against live rules, candidate_q()'s SUPERSET/equivalence with matches() over
real rows (including a non-normalized mixed-case domain), and the model.clean()
enforcement paths used by admin.
"""
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

import tenants.resolve_cache as rc
from tenants.models import Domain, ReservedHostRule, Shard, Tenant
from tenants.serializers import TenantSerializer
from tenants.validators import validate_tenant_domain, validate_tenant_schema_name
from tenants.views import BaseDomainsView
from users.models import User

SEED_LABELS = {"www", "api", "admin", "mail", "staging",
               "dev", "test", "status", "docs", "support"}
SEED_APEXES = {"routegenie.com", "isi-technology.com"}


class SeedMigrationTests(TestCase):
    def test_global_labels_seeded(self):
        got = set(
            ReservedHostRule.objects
            .filter(match_type=ReservedHostRule.MatchType.LABEL, base_domain="", is_active=True)
            .values_list("value", flat=True)
        )
        self.assertTrue(SEED_LABELS <= got, f"missing: {SEED_LABELS - got}")

    def test_apexes_seeded_as_exact(self):
        got = set(
            ReservedHostRule.objects
            .filter(match_type=ReservedHostRule.MatchType.EXACT, is_active=True)
            .values_list("value", flat=True)
        )
        self.assertTrue(SEED_APEXES <= got, f"missing: {SEED_APEXES - got}")


class ValidateAgainstSeededRulesTests(TestCase):
    """validate_* run their real DB queries against the seeded rules."""

    def test_reserved_domain_rejected(self):
        for host in ["www.acme.com", "api.foo.io", "admin.bar.net", "routegenie.com"]:
            with self.assertRaises(ValidationError):
                validate_tenant_domain(host)

    def test_allowed_domain_ok(self):
        self.assertEqual(validate_tenant_domain("acme.client.com"), "acme.client.com")

    def test_reserved_schema_rejected(self):
        for name in ["www", "api", "admin"]:
            with self.assertRaises(ValidationError):
                validate_tenant_schema_name(name)

    def test_allowed_schema_ok(self):
        self.assertEqual(validate_tenant_schema_name("acme"), "acme")


class CandidateQSupersetDBTests(TestCase):
    """candidate_q() must be a SUPERSET of matches() in real SQL, and matches() must
    confirm it back to the exact set — including a non-normalized mixed-case domain
    (the case-insensitivity guarantee) that a case-sensitive prefilter would miss."""

    @classmethod
    def setUpTestData(cls):
        default = Shard.objects.create(alias="default", name="Default", is_default=True, is_active=True)
        s1 = Shard.objects.create(alias="tenant_1", name="T1", is_default=False, is_active=True)
        t = Tenant.objects.create(schema_name="acme", company_name="Acme", shard=s1, status=Tenant.Status.ACTIVE)
        cls.hosts = [
            "www.acme.com", "WWW.MixedCase.com", "api.acme.com",
            "admin.routegenie.com", "admin.client.com", "acme.routegenie.com",
            "internal.x.com", "x.internal.x.com", "manage.localhost", "alpha.company1.com",
        ]
        for i, h in enumerate(cls.hosts):
            # NOTE: .create() bypasses Domain.clean(), so "WWW.MixedCase.com" is stored
            # non-normalized on purpose (simulates a CLI-created domain).
            Domain.objects.create(domain=h, tenant=t, is_primary=(i == 0))

    def test_superset_and_confirm_equivalence(self):
        rules = [
            ReservedHostRule(match_type=ReservedHostRule.MatchType.LABEL, value="www"),
            ReservedHostRule(match_type=ReservedHostRule.MatchType.LABEL,
                             value="admin", base_domain="routegenie.com"),
            ReservedHostRule(match_type=ReservedHostRule.MatchType.EXACT, value="manage.localhost"),
            ReservedHostRule(match_type=ReservedHostRule.MatchType.SUFFIX, value="internal.x.com"),
        ]
        all_domains = list(Domain.objects.all())
        for r in rules:
            brute = {d.domain for d in all_domains if r.matches(d.domain)}
            candidates = set(
                Domain.objects.filter(r.candidate_q()).values_list("domain", flat=True)
            )
            confirmed = {h for h in candidates if r.matches(h)}
            self.assertTrue(brute <= candidates, f"{r}: SUPERSET broken, lost {brute - candidates}")
            self.assertEqual(brute, confirmed, f"{r}: confirm mismatch")

    def test_mixed_case_domain_is_found(self):
        r = ReservedHostRule(match_type=ReservedHostRule.MatchType.LABEL, value="www")
        candidates = set(Domain.objects.filter(r.candidate_q()).values_list("domain", flat=True))
        self.assertIn("WWW.MixedCase.com", candidates)   # istartswith, not startswith


class ModelCleanIntegrationTests(TestCase):
    """The enforcement paths admin funnels through: Domain.clean() / Tenant.clean()."""

    @classmethod
    def setUpTestData(cls):
        cls.default = Shard.objects.create(alias="default", name="Default", is_default=True, is_active=True)
        cls.s1 = Shard.objects.create(alias="tenant_1", name="T1", is_default=False, is_active=True)
        cls.tenant = Tenant.objects.create(
            schema_name="acme", company_name="Acme", shard=cls.s1, status=Tenant.Status.ACTIVE)
        cls.public = Tenant.objects.create(
            schema_name="public", company_name="Public", shard=cls.default, status=Tenant.Status.ACTIVE)

    def test_domain_full_clean_rejects_reserved(self):
        d = Domain(domain="api.acme.com", tenant=self.tenant, is_primary=False)
        with self.assertRaises(ValidationError):
            d.full_clean()

    def test_domain_full_clean_allows_ok(self):
        d = Domain(domain="portal.acme.com", tenant=self.tenant, is_primary=False)
        d.full_clean()   # must not raise

    def test_public_tenant_domain_is_exempt(self):
        # routegenie.com is a reserved apex, but the public tenant is exempt.
        d = Domain(domain="routegenie.com", tenant=self.public, is_primary=False)
        d.full_clean()   # must not raise

    def test_tenant_clean_rejects_reserved_schema_on_create(self):
        t = Tenant(schema_name="admin", company_name="AdminCo", shard=self.s1, status=Tenant.Status.NEW)
        with self.assertRaises(ValidationError):
            t.full_clean()

    def test_tenant_clean_allows_ok_schema(self):
        t = Tenant(schema_name="freshco", company_name="FreshCo", shard=self.s1, status=Tenant.Status.NEW)
        t.full_clean()   # must not raise


class TenantUpdateDBTests(TestCase):
    """TenantSerializer update path: immutable schema_name, company_name rename/unique,
    description, and primary-domain repoint with OLD-host cache invalidation.

    Only the 'default' connection is touched (Tenant/Shard/Domain are shared-app), so
    the fake 'tenant_1' shard alias never opens a connection. We never read
    serializer.data (that would trigger get_admins -> tenant_context on the shard)."""

    @classmethod
    def setUpTestData(cls):
        Shard.objects.create(alias="default", name="Default", is_default=True, is_active=True)
        cls.s1 = Shard.objects.create(alias="tenant_1", name="T1", is_default=False, is_active=True)
        cls.t = Tenant.objects.create(
            schema_name="acme", company_name="Acme", shard=cls.s1, status=Tenant.Status.ACTIVE)
        Domain.objects.create(domain="acme.client.com", tenant=cls.t, is_primary=True)

    def _update(self, data):
        s = TenantSerializer(self.t, data=data, partial=True)
        s.is_valid(raise_exception=True)
        return s.save()

    def test_schema_name_is_immutable(self):
        self._update({"company_name": "Acme 2", "schema_name": "hacked"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.schema_name, "acme")      # read-only: change ignored
        self.assertEqual(self.t.company_name, "Acme 2")

    def test_description_updates(self):
        self._update({"description": "some notes"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.description, "some notes")

    def test_description_length_capped(self):
        s = TenantSerializer(self.t, data={"description": "x" * 301}, partial=True)
        self.assertFalse(s.is_valid())
        self.assertIn("description", s.errors)

    def test_domain_repoint_invalidates_old_host(self):
        with mock.patch.object(rc.resolve_cache, "forget_host") as fh:
            self._update({"domain": "portal.client.com"})
        self.assertEqual(self.t.domains.get(is_primary=True).domain, "portal.client.com")
        called = [c.args[0] for c in fh.call_args_list]
        self.assertIn("acme.client.com", called)          # OLD host explicitly invalidated

    def test_company_name_unique(self):
        Tenant.objects.create(
            schema_name="beta", company_name="Beta", shard=self.s1, status=Tenant.Status.ACTIVE)
        s = TenantSerializer(self.t, data={"company_name": "Beta"}, partial=True)
        self.assertFalse(s.is_valid())
        self.assertIn("company_name", s.errors)

    def test_repoint_to_own_secondary_domain_is_rejected(self):
        # A secondary domain of the SAME tenant is a real UNIQUE collision for a
        # repoint — must be a friendly 400, not a 500 IntegrityError on save.
        Domain.objects.create(domain="shop.client.com", tenant=self.t, is_primary=False)
        s = TenantSerializer(self.t, data={"domain": "shop.client.com"}, partial=True)
        self.assertFalse(s.is_valid())
        self.assertIn("domain", s.errors)

    def test_repoint_to_current_primary_is_noop(self):
        # Re-submitting the current primary must pass validation (excluded by pk).
        s = TenantSerializer(self.t, data={"domain": "acme.client.com"}, partial=True)
        self.assertTrue(s.is_valid(), s.errors)


class BeatSchemaFilterDBTests(TestCase):
    """beat schedules ONLY ACTIVE tenants — NEW/PENDING/FAILED/DEACTIVATED are skipped
    (their schemas may be unprovisioned/half-migrated, which would crash the scheduler)."""

    def test_only_active_tenants_returned(self):
        from django_tenants.utils import get_public_schema_name
        from tenants.celery.db_scheduler import TenantAwareDatabaseScheduler

        Shard.objects.create(alias="default", name="Default", is_default=True, is_active=True)
        s1 = Shard.objects.create(alias="tenant_1", name="T1", is_default=False, is_active=True)
        Tenant.objects.create(schema_name="act", company_name="Act", shard=s1, status=Tenant.Status.ACTIVE)
        Tenant.objects.create(schema_name="newt", company_name="Newt", shard=s1, status=Tenant.Status.NEW)
        Tenant.objects.create(schema_name="deact", company_name="Deact", shard=s1, status=Tenant.Status.DEACTIVATED)
        Tenant.objects.create(schema_name="failt", company_name="Failt", shard=s1, status=Tenant.Status.FAILED)

        # self is unused by the method; call unbound to avoid constructing a scheduler.
        names = TenantAwareDatabaseScheduler.get_tenant_schema_names(None, [get_public_schema_name()])
        self.assertIn("act", names)
        for excluded in ("newt", "deact", "failt"):
            self.assertNotIn(excluded, names)


class BaseDomainsEndpointDBTests(TestCase):
    """Permission gating for GET /api/base-domains/ (needs a User row → DB)."""

    def test_tenant_admin_gets_200(self):
        admin = User.objects.create_user(username="root", password="pw")
        admin.role = User.Role.TENANT_ADMIN
        admin.save()
        req = APIRequestFactory().get("/api/base-domains/")
        force_authenticate(req, user=admin)
        resp = BaseDomainsView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("base_domains", resp.data)

    def test_anonymous_is_denied(self):
        req = APIRequestFactory().get("/api/base-domains/")
        resp = BaseDomainsView.as_view()(req)
        self.assertIn(resp.status_code, (401, 403))
