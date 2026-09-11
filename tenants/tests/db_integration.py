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
from typing import Any
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connections, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

import tenants.resolver as rc
from tenants.models import Domain, ReservedHostRule, Shard, TaskRun, Tenant
from tenants.console.serializers import TenantSerializer
from tenants.validators import (
    quote_schema,
    validate_schema_name,
    validate_tenant_domain,
    validate_tenant_schema_name,
)
from tenants.context import tenant_context
from tenants.console.views import BaseDomainsView, TenantViewSet
from users.models import User

SEED_LABELS = {"www", "api", "admin", "mail", "staging",
               "dev", "test", "status", "docs", "support"}
SEED_APEXES = {"routegenie.com", "isi-technology.com"}


class SeedMigrationTests(TestCase):
    def test_global_labels_seeded(self) -> None:
        got = set(
            ReservedHostRule.objects
            .filter(match_type=ReservedHostRule.MatchType.LABEL, base_domain="", is_active=True)
            .values_list("value", flat=True)
        )
        self.assertTrue(SEED_LABELS <= got, f"missing: {SEED_LABELS - got}")

    def test_apexes_seeded_as_exact(self) -> None:
        got = set(
            ReservedHostRule.objects
            .filter(match_type=ReservedHostRule.MatchType.EXACT, is_active=True)
            .values_list("value", flat=True)
        )
        self.assertTrue(SEED_APEXES <= got, f"missing: {SEED_APEXES - got}")


class ValidateAgainstSeededRulesTests(TestCase):
    """validate_* run their real DB queries against the seeded rules."""

    def test_reserved_domain_rejected(self) -> None:
        for host in ["www.acme.com", "api.foo.io", "admin.bar.net", "routegenie.com"]:
            with self.assertRaises(ValidationError):
                validate_tenant_domain(host)

    def test_allowed_domain_ok(self) -> None:
        self.assertEqual(validate_tenant_domain("acme.client.com"), "acme.client.com")

    def test_reserved_schema_rejected(self) -> None:
        for name in ["www", "api", "admin"]:
            with self.assertRaises(ValidationError):
                validate_tenant_schema_name(name)

    def test_allowed_schema_ok(self) -> None:
        self.assertEqual(validate_tenant_schema_name("acme"), "acme")


class CandidateQSupersetDBTests(TestCase):
    """candidate_q() must be a SUPERSET of matches() in real SQL, and matches() must
    confirm it back to the exact set — including a non-normalized mixed-case domain
    (the case-insensitivity guarantee) that a case-sensitive prefilter would miss."""

    @classmethod
    def setUpTestData(cls) -> None:
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

    def test_superset_and_confirm_equivalence(self) -> None:
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

    def test_mixed_case_domain_is_found(self) -> None:
        r = ReservedHostRule(match_type=ReservedHostRule.MatchType.LABEL, value="www")
        candidates = set(Domain.objects.filter(r.candidate_q()).values_list("domain", flat=True))
        self.assertIn("WWW.MixedCase.com", candidates)   # istartswith, not startswith


class ModelCleanIntegrationTests(TestCase):
    """The enforcement paths admin funnels through: Domain.clean() / Tenant.clean()."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.default = Shard.objects.create(alias="default", name="Default", is_default=True, is_active=True)
        cls.s1 = Shard.objects.create(alias="tenant_1", name="T1", is_default=False, is_active=True)
        cls.tenant = Tenant.objects.create(
            schema_name="acme", company_name="Acme", shard=cls.s1, status=Tenant.Status.ACTIVE)
        cls.public = Tenant.objects.create(
            schema_name="public", company_name="Public", shard=cls.default, status=Tenant.Status.ACTIVE)

    def test_domain_full_clean_rejects_reserved(self) -> None:
        d = Domain(domain="api.acme.com", tenant=self.tenant, is_primary=False)
        with self.assertRaises(ValidationError):
            d.full_clean()

    def test_domain_full_clean_allows_ok(self) -> None:
        d = Domain(domain="portal.acme.com", tenant=self.tenant, is_primary=False)
        d.full_clean()   # must not raise

    def test_public_tenant_domain_is_exempt(self) -> None:
        # routegenie.com is a reserved apex, but the public tenant is exempt.
        d = Domain(domain="routegenie.com", tenant=self.public, is_primary=False)
        d.full_clean()   # must not raise

    def test_tenant_clean_rejects_reserved_schema_on_create(self) -> None:
        t = Tenant(schema_name="admin", company_name="AdminCo", shard=self.s1, status=Tenant.Status.NEW)
        with self.assertRaises(ValidationError):
            t.full_clean()

    def test_tenant_clean_allows_ok_schema(self) -> None:
        t = Tenant(schema_name="freshco", company_name="FreshCo", shard=self.s1, status=Tenant.Status.NEW)
        t.full_clean()   # must not raise


class TenantUpdateDBTests(TestCase):
    """TenantSerializer update path: immutable schema_name, company_name rename/unique,
    description, and primary-domain repoint with OLD-host cache invalidation.

    Only the 'default' connection is touched (Tenant/Shard/Domain are shared-app), so
    the fake 'tenant_1' shard alias never opens a connection. We never read
    serializer.data (that would trigger get_admins -> tenant_context on the shard)."""

    @classmethod
    def setUpTestData(cls) -> None:
        Shard.objects.create(alias="default", name="Default", is_default=True, is_active=True)
        cls.s1 = Shard.objects.create(alias="tenant_1", name="T1", is_default=False, is_active=True)
        cls.t = Tenant.objects.create(
            schema_name="acme", company_name="Acme", shard=cls.s1, status=Tenant.Status.ACTIVE)
        Domain.objects.create(domain="acme.client.com", tenant=cls.t, is_primary=True)

    def _update(self, data: dict[str, Any]) -> Any:
        s = TenantSerializer(self.t, data=data, partial=True)
        s.is_valid(raise_exception=True)
        return s.save()

    def test_schema_name_is_immutable(self) -> None:
        self._update({"company_name": "Acme 2", "schema_name": "hacked"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.schema_name, "acme")      # read-only: change ignored
        self.assertEqual(self.t.company_name, "Acme 2")

    def test_description_updates(self) -> None:
        self._update({"description": "some notes"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.description, "some notes")

    def test_description_length_capped(self) -> None:
        s = TenantSerializer(self.t, data={"description": "x" * 301}, partial=True)
        self.assertFalse(s.is_valid())
        self.assertIn("description", s.errors)

    def test_domain_repoint_invalidates_old_host(self) -> None:
        with mock.patch.object(rc.resolve_cache, "forget_host") as fh:
            self._update({"domain": "portal.client.com"})
        self.assertEqual(self.t.domains.get(is_primary=True).domain, "portal.client.com")
        called = [c.args[0] for c in fh.call_args_list]
        self.assertIn("acme.client.com", called)          # OLD host explicitly invalidated

    def test_company_name_unique(self) -> None:
        Tenant.objects.create(
            schema_name="beta", company_name="Beta", shard=self.s1, status=Tenant.Status.ACTIVE)
        s = TenantSerializer(self.t, data={"company_name": "Beta"}, partial=True)
        self.assertFalse(s.is_valid())
        self.assertIn("company_name", s.errors)

    def test_repoint_to_own_secondary_domain_is_rejected(self) -> None:
        # A secondary domain of the SAME tenant is a real UNIQUE collision for a
        # repoint — must be a friendly 400, not a 500 IntegrityError on save.
        Domain.objects.create(domain="shop.client.com", tenant=self.t, is_primary=False)
        s = TenantSerializer(self.t, data={"domain": "shop.client.com"}, partial=True)
        self.assertFalse(s.is_valid())
        self.assertIn("domain", s.errors)

    def test_repoint_to_current_primary_is_noop(self) -> None:
        # Re-submitting the current primary must pass validation (excluded by pk).
        s = TenantSerializer(self.t, data={"domain": "acme.client.com"}, partial=True)
        self.assertTrue(s.is_valid(), s.errors)


class FanoutTargetsDBTests(TestCase):
    """The fanout enumeration helpers: interval targets = ACTIVE non-public tenants;
    calendar targets = those that ALSO have a configured timezone (NULL tz skipped)."""

    def test_active_target_and_tz_filtering(self) -> None:
        from commons.platform.tenancy import active_target_schemas, active_tenants_with_tz

        d = Shard.objects.create(alias="default", name="Default", is_default=True, is_active=True)
        s1 = Shard.objects.create(alias="tenant_1", name="T1", is_default=False, is_active=True)
        Tenant.objects.create(schema_name="public", company_name="Public", shard=d,
                              status=Tenant.Status.ACTIVE, timezone="UTC")
        Tenant.objects.create(schema_name="act", company_name="Act", shard=s1,
                              status=Tenant.Status.ACTIVE, timezone="Europe/Kyiv")
        Tenant.objects.create(schema_name="act2", company_name="Act2", shard=s1,
                              status=Tenant.Status.ACTIVE)                      # NULL tz
        Tenant.objects.create(schema_name="newt", company_name="Newt", shard=s1,
                              status=Tenant.Status.NEW)
        Tenant.objects.create(schema_name="deact", company_name="Deact", shard=s1,
                              status=Tenant.Status.DEACTIVATED)

        # interval: all ACTIVE tenants, excluding public; NULL tz still included
        self.assertEqual(set(active_target_schemas("tenants")), {"act", "act2"})
        # calendar: only ACTIVE tenants WITH a tz (act2 NULL-tz + public excluded)
        self.assertEqual({s for s, _ in active_tenants_with_tz()}, {"act"})


class BaseDomainsEndpointDBTests(TestCase):
    """Permission gating for GET /api/base-domains/ (needs a User row → DB)."""

    def test_tenant_admin_gets_200(self) -> None:
        admin = User.objects.create_user(username="root", password="pw")
        admin.role = User.Role.TENANT_ADMIN
        admin.save()
        req = APIRequestFactory().get("/api/base-domains/")
        force_authenticate(req, user=admin)
        resp = BaseDomainsView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("base_domains", resp.data)

    def test_anonymous_is_denied(self) -> None:
        req = APIRequestFactory().get("/api/base-domains/")
        resp = BaseDomainsView.as_view()(req)
        self.assertIn(resp.status_code, (401, 403))


class TaskRunTests(TestCase):
    """Durable per-(task, schema) watermark: bulk upsert + load_map round-trip."""

    SIG = ""                  # argsig(None) — the no-args schedule entry

    def test_mark_ran_upserts_and_load_map_reads(self) -> None:
        t1 = timezone.now().replace(microsecond=0)
        TaskRun.mark_ran("app.daily", self.SIG, ["a", "b"], t1)
        self.assertEqual(set(TaskRun.load_map("app.daily", self.SIG)), {"a", "b"})

        t2 = t1 + timedelta(hours=1)
        TaskRun.mark_ran("app.daily", self.SIG, ["a"], t2)      # upsert 'a', leave 'b'
        m = TaskRun.load_map("app.daily", self.SIG)
        self.assertEqual(m["a"], t2)
        self.assertEqual(m["b"], t1)

    def test_mark_ran_parses_iso_string(self) -> None:
        t = timezone.now().replace(microsecond=0)
        TaskRun.mark_ran("app.weekly", self.SIG, ["c"], t.isoformat())   # run_ts as ISO
        self.assertEqual(TaskRun.load_map("app.weekly", self.SIG)["c"], t)

    def test_load_map_is_scoped_per_task(self) -> None:
        now = timezone.now()
        TaskRun.mark_ran("task.x", self.SIG, ["a"], now)
        TaskRun.mark_ran("task.y", self.SIG, ["b"], now)
        self.assertEqual(set(TaskRun.load_map("task.x", self.SIG)), {"a"})


class SchemaNameLifecycleTests(TestCase):
    """The end-to-end claim behind allowing a LEADING DIGIT in a schema name.

    `1st_choice` is a legitimate company name, but PostgreSQL cannot reference such a
    schema unquoted — so allowing it is only safe if EVERY site quotes. The DB-free suite
    pins the validators; this pins the thing they cannot: that such a schema actually
    survives CREATE -> migrate -> read back -> DROP against a real PostgreSQL.
    """

    def _shard(self) -> Shard:
        alias = next(a for a in settings.DATABASES if a != "default")
        return Shard.objects.get_or_create(
            alias=alias, defaults={"name": alias, "is_active": True})[0]

    def test_leading_digit_schema_survives_create_and_drop(self) -> None:
        schema = validate_schema_name("1st-Choice")          # -> "1st_choice"
        self.assertEqual(schema, "1st_choice")
        shard = self._shard()
        conn = connections[shard.alias]

        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {quote_schema(schema)}")
        try:
            # It really exists, under the name we asked for (case- and char-exact).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name = %s", [schema])
                self.assertEqual(cur.fetchone()[0], schema)
            # And search_path accepts it — this is the step a hyphenated or mixed-case
            # name would only survive because upstream single-quotes the value.
            with conn.cursor() as cur:
                cur.execute(f"SET search_path = '{schema}'")
                cur.execute("SHOW search_path")
                self.assertIn(schema, cur.fetchone()[0])
        finally:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA {quote_schema(schema)} CASCADE")
                cur.execute("SET search_path = 'public'")

    def test_injectable_schema_name_never_reaches_sql(self) -> None:
        """quote_schema is the last line: a name that predates validation (data migration,
        manual INSERT) must abort BEFORE the cursor, not produce two statements."""
        with self.assertRaises(ValueError):
            quote_schema('x";DROP SCHEMA public CASCADE;--')
        # public is still there, i.e. nothing was executed on the way to the exception.
        with connections["default"].cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.schemata "
                        "WHERE schema_name = 'public'")
            self.assertIsNotNone(cur.fetchone())

    def test_serializer_and_admin_agree_on_the_same_name(self) -> None:
        """The defect that started this: the API normalized+validated while the admin
        (Tenant.clean) did neither, so the two disagreed about what a valid tenant is."""
        shard = self._shard()
        ser = TenantSerializer(data={"schema_name": "Freedom-First",
                                     "company_name": "Freedom First",
                                     "shard_id": shard.pk, "domain": "ff.example.com"})
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(ser.validated_data["schema_name"], "freedom_first")

        t = Tenant(schema_name="Freedom-First", company_name="Other", shard=shard)
        t.clean()
        self.assertEqual(t.schema_name, "freedom_first")     # same answer, both paths


class TaskRunArgsSigTests(TestCase):
    """The (schema, task, args_sig) uniqueness key, against the real constraint.

    Two calendar entries sharing a task name but differing by args are independent
    schedules; with the key on (schema, task) alone the second one's watermark collides
    with the first's and its occurrence reads as already run.
    """

    def test_same_task_different_args_keep_separate_watermarks(self) -> None:
        t1 = timezone.now().replace(microsecond=0)
        t2 = t1 + timedelta(hours=1)
        TaskRun.mark_ran("app.fetch", "sig_one", ["alpha"], t1)
        TaskRun.mark_ran("app.fetch", "sig_two", ["alpha"], t2)

        self.assertEqual(TaskRun.load_map("app.fetch", "sig_one"), {"alpha": t1})
        self.assertEqual(TaskRun.load_map("app.fetch", "sig_two"), {"alpha": t2})
        self.assertEqual(TaskRun.objects.filter(task="app.fetch").count(), 2)

    def test_upsert_is_scoped_to_one_signature(self) -> None:
        t1 = timezone.now().replace(microsecond=0)
        t2 = t1 + timedelta(hours=1)
        TaskRun.mark_ran("app.fetch", "sig_one", ["alpha"], t1)
        TaskRun.mark_ran("app.fetch", "sig_two", ["alpha"], t1)
        TaskRun.mark_ran("app.fetch", "sig_one", ["alpha"], t2)      # advance only sig_one

        self.assertEqual(TaskRun.load_map("app.fetch", "sig_one"), {"alpha": t2})
        self.assertEqual(TaskRun.load_map("app.fetch", "sig_two"), {"alpha": t1})

    def test_task_index_still_spans_every_args_variant(self) -> None:
        """The `task`-only index serves the operator question "why did app.fetch not fire
        for tenant X", which must see all variants."""
        now = timezone.now()
        TaskRun.mark_ran("app.fetch", "sig_one", ["alpha", "beta"], now)
        TaskRun.mark_ran("app.fetch", "sig_two", ["alpha"], now)
        self.assertEqual(TaskRun.objects.filter(task="app.fetch").count(), 3)


class DomainCanonicalConstraintTests(TestCase):
    """tenants_domain_canonical — the invariant candidate_q()'s direct comparison rests on,
    and the reason a non-canonical hostname can no longer become an unreachable tenant.

    Inserted with bulk_create on purpose: that bypasses Domain.save() (and its
    normalization) exactly the way a data migration would, so it is the path the constraint
    exists to police.
    """

    def setUp(self) -> None:
        shard = Shard.objects.get_or_create(
            alias=next(a for a in settings.DATABASES if a != "default"),
            defaults={"is_active": True})[0]
        self.tenant = Tenant.objects.create(
            schema_name="canon", company_name="Canon", shard=shard)

    def _bulk(self, domain: str) -> None:
        Domain.objects.bulk_create(
            [Domain(domain=domain, tenant=self.tenant, is_primary=False)])

    def test_canonical_domains_are_accepted(self) -> None:
        for d in ["acme.com", "x.acme.com", "a-b.acme.com"]:
            with self.subTest(domain=d):
                with transaction.atomic():
                    self._bulk(d)
                self.assertTrue(Domain.objects.filter(domain=d).exists())

    def test_non_canonical_domains_are_refused(self) -> None:
        """Upper case, trailing dot, surrounding whitespace — each would make the tenant
        unreachable (request.get_host() is compared exactly) and would break
        candidate_q()'s superset contract."""
        for d in ["ACME.COM", "acme.com.", "acme.com..", " acme.com", "acme.com "]:
            with self.subTest(domain=d):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._bulk(d)

    def test_save_still_normalizes_so_the_normal_path_never_trips_it(self) -> None:
        d = Domain.objects.create(domain="  ACME.COM.  ", tenant=self.tenant,
                                  is_primary=False)
        d.refresh_from_db()
        self.assertEqual(d.domain, "acme.com")

    def test_constraint_allows_exactly_the_fixed_points_of_normalize_host(self) -> None:
        """The constraint and normalize_host must describe the same set, because
        candidate_q() compares a normalize_host()-ed value against the raw column."""
        from tenants.validators import normalize_host
        for d in ["acme.com", "ACME.COM", "acme.com.", "acme.com ", " acme.com"]:
            with self.subTest(domain=d):
                is_fixed_point = normalize_host(d) == d
                try:
                    with transaction.atomic():
                        self._bulk(d)
                    accepted = True
                except IntegrityError:
                    accepted = False
                self.assertEqual(accepted, is_fixed_point)


class CandidateQAgainstRealRowsTests(TestCase):
    """candidate_q()/matches() equivalence against REAL rows and real SQL.

    The DB-free sibling (test_reserved_hosts.CandidateQSupersetTests) simulates Postgres;
    this is the only place the actual query plan is exercised. All fixtures are canonical —
    non-canonical rows cannot exist (see DomainCanonicalConstraintTests).
    """

    CANONICAL = ["acme.com", "x.acme.com", "www.acme.com", "y.x.acme.com", "other.com"]

    def setUp(self) -> None:
        shard = Shard.objects.get_or_create(
            alias=next(a for a in settings.DATABASES if a != "default"),
            defaults={"is_active": True})[0]
        tenant = Tenant.objects.create(schema_name="dots", company_name="Dots", shard=shard)
        Domain.objects.bulk_create([Domain(domain=d, tenant=tenant, is_primary=False)
                                    for d in self.CANONICAL])

    def _assert_superset(self, rule: Any) -> None:
        candidates = set(
            Domain.objects.filter(rule.candidate_q()).values_list("domain", flat=True))
        truth = {d for d in self.CANONICAL if rule.matches(d)}
        self.assertTrue(truth, "fixture should produce at least one true match")
        self.assertEqual(truth - candidates, set(),
                         f"{rule}: candidate_q EXCLUDED true match(es)")

    def test_exact_rule_superset_holds(self) -> None:
        self._assert_superset(
            ReservedHostRule(match_type=ReservedHostRule.MatchType.EXACT, value="acme.com"))

    def test_suffix_rule_superset_holds(self) -> None:
        self._assert_superset(
            ReservedHostRule(match_type=ReservedHostRule.MatchType.SUFFIX, value="acme.com"))

    def test_label_rule_superset_holds(self) -> None:
        self._assert_superset(ReservedHostRule(
            match_type=ReservedHostRule.MatchType.LABEL, value="www", base_domain=""))


class AdminsProbeAgainstRealSchemasTests(TestCase):
    """_admins_for against real per-schema users tables.

    The DB-free suite mocks the cursor, so it pins the SHAPE (one pair of queries per
    shard, quoting, per-branch role parameter) but not the SQL itself. This is the only
    place the cross-schema UNION actually runs — and the only place that proves the batched
    probe returns what the old per-tenant tenant_context() query returned.
    """

    def setUp(self) -> None:
        self.alias = next(a for a in settings.DATABASES if a != "default")
        shard = Shard.objects.get_or_create(
            alias=self.alias, defaults={"is_active": True})[0]
        self.tenants = [
            Tenant.objects.create(schema_name=s, company_name=s.title(), shard=shard)
            for s in ("probe_a", "probe_b")
        ]
        # Real schemas + the users table in each, via the normal provisioning path.
        for t in self.tenants:
            call_command("migrate_schemas", tenant=True, schema_name=t.schema_name,
                         verbosity=0)
        with tenant_context(self.tenants[0]):
            User.objects.create_user(username="root_a", password="x",
                                     role=User.Role.COMPANY_ADMIN)
            User.objects.create_user(username="cust_a", password="x",
                                     role=User.Role.CUSTOMER)   # must NOT be listed

    def test_returns_only_company_admins_of_the_right_schema(self) -> None:
        got = TenantViewSet._admins_for(self.tenants)
        self.assertEqual([a["username"] for a in got[(self.alias, "probe_a")]], ["root_a"])
        self.assertNotIn((self.alias, "probe_b"), got)     # no admins there yet

    def test_matches_what_a_per_tenant_query_would_return(self) -> None:
        """Equivalence with the path this replaced."""
        batched = TenantViewSet._admins_for(self.tenants)
        for t in self.tenants:
            with tenant_context(t):
                expected = list(User.objects.filter(role=User.Role.COMPANY_ADMIN)
                                .order_by("username")
                                .values("id", "username", "is_active"))
            self.assertEqual(batched.get((self.alias, t.schema_name), []), expected)

    def test_tenant_without_a_migrated_schema_is_simply_absent(self) -> None:
        shard = Shard.objects.get(alias=self.alias)
        ghost = Tenant.objects.create(schema_name="probe_ghost",
                                      company_name="Ghost", shard=shard)
        got = TenantViewSet._admins_for(self.tenants + [ghost])
        self.assertNotIn((self.alias, "probe_ghost"), got)
