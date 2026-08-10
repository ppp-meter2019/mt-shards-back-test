"""Reserved-host rules: matching semantics + domain/schema validation.

DB-free (SimpleTestCase), like the rest of this suite:
  * ReservedHostRule.matches() operates on an in-memory instance — no query.
  * The hostname/label format validators are pure string checks.
  * The DB-touching glue (validate_tenant_domain / validate_tenant_schema_name)
    is exercised with its DB helpers mocked, so the wiring is covered without
    Postgres. End-to-end API/seed behavior belongs to the DB-backed harness.
"""
import io
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from tenants import validators as V
from tenants.management.commands import bootstrap_tenant as bt_mod
from tenants.models import ReservedHostRule as R


def rule(match_type, value, base=""):
    return R(match_type=match_type, value=value, base_domain=base, is_active=True)


class MatchesTests(SimpleTestCase):
    def test_exact(self):
        r = rule(R.MatchType.EXACT, "manage.routegenie.com")
        self.assertTrue(r.matches("manage.routegenie.com"))
        self.assertTrue(r.matches("MANAGE.routegenie.com"))     # case-insensitive
        self.assertTrue(r.matches("manage.routegenie.com."))    # trailing dot
        self.assertFalse(r.matches("x.manage.routegenie.com"))
        self.assertFalse(r.matches("routegenie.com"))

    def test_suffix(self):
        r = rule(R.MatchType.SUFFIX, "internal.routegenie.com")
        self.assertTrue(r.matches("internal.routegenie.com"))       # the host itself
        self.assertTrue(r.matches("x.internal.routegenie.com"))     # a subdomain
        self.assertFalse(r.matches("routegenie.com"))
        self.assertFalse(r.matches("notinternal.routegenie.com"))   # not a dotted boundary

    def test_label_global(self):
        r = rule(R.MatchType.LABEL, "www")                          # base empty => global
        self.assertTrue(r.matches("www.routegenie.com"))
        self.assertTrue(r.matches("www.client.com"))                # blocks custom domains too
        self.assertFalse(r.matches("api.routegenie.com"))           # different label
        self.assertFalse(r.matches("alpha.company1.com"))

    def test_label_scoped_to_base(self):
        r = rule(R.MatchType.LABEL, "admin", base="routegenie.com")
        self.assertTrue(r.matches("admin.routegenie.com"))
        self.assertFalse(r.matches("admin.client.com"))             # outside the base


class HostnameValidatorTests(SimpleTestCase):
    def test_accepts(self):
        for host in ["main.somedomain.com", "alpha.company200.com", "localhost",
                     "a-b.example.co.uk"]:
            self.assertEqual(V.validate_hostname(host), host)

    def test_normalizes_case_and_trailing_dot(self):
        self.assertEqual(V.validate_hostname("  ACME.RouteGenie.com. "), "acme.routegenie.com")

    def test_rejects(self):
        for bad in ["-bad.com", "a..b", "bad_underscore.com", "", "x" * 64 + ".com"]:
            with self.assertRaises(ValidationError):
                V.validate_hostname(bad)

    def test_rejects_numeric_tld_and_ip(self):
        for bad in ["1.2.3.4", "999", "example.123", "0.0.0.0"]:
            with self.assertRaises(ValidationError):
                V.validate_hostname(bad)

    def test_rejects_overlong_host(self):
        with self.assertRaises(ValidationError):
            V.validate_hostname(".".join(["a"] * 200))   # 399 chars > 253


class LabelValidatorTests(SimpleTestCase):
    def test_accepts_and_normalizes(self):
        self.assertEqual(V.validate_label("WWW"), "www")

    def test_rejects_dotted_or_empty(self):
        for bad in ["has.dot", "", "-lead", "trail-", "a" * 64]:
            with self.assertRaises(ValidationError):
                V.validate_label(bad)


class DenialMessageTests(SimpleTestCase):
    def test_messages(self):
        self.assertIn("Subdomain 'www'", rule(R.MatchType.LABEL, "www").denial_message("www.x.com"))
        self.assertIn("under routegenie.com",
                      rule(R.MatchType.LABEL, "admin", base="routegenie.com").denial_message("admin.routegenie.com"))
        self.assertIn("under 'internal.x.com'",
                      rule(R.MatchType.SUFFIX, "internal.x.com").denial_message("a.internal.x.com"))
        self.assertIn("Host 'm.x.com'", rule(R.MatchType.EXACT, "m.x.com").denial_message("m.x.com"))


class NormalizeTests(SimpleTestCase):
    """The single source of truth shared by Model.clean() and the serializer."""

    def test_label_global(self):
        self.assertEqual(R.normalize(R.MatchType.LABEL, "WWW", ""), ("www", ""))

    def test_label_scoped_normalizes_base(self):
        self.assertEqual(
            R.normalize(R.MatchType.LABEL, "www", "RouteGenie.com."), ("www", "routegenie.com"))

    def test_exact_forces_base_empty(self):
        self.assertEqual(
            R.normalize(R.MatchType.EXACT, "Manage.RouteGenie.com", "ignored"),
            ("manage.routegenie.com", ""))

    def test_suffix_forces_base_empty(self):
        self.assertEqual(
            R.normalize(R.MatchType.SUFFIX, "internal.x.com", "x"), ("internal.x.com", ""))

    def test_label_rejects_dotted_value(self):
        with self.assertRaises(ValidationError):
            R.normalize(R.MatchType.LABEL, "has.dot", "")

    def test_host_type_rejects_bad_value(self):
        with self.assertRaises(ValidationError):
            R.normalize(R.MatchType.EXACT, "-bad", "")

    def test_unknown_match_type_rejected(self):
        with self.assertRaises(ValidationError):
            R.normalize("nope", "x", "")


class ValidateTenantDomainGlueTests(SimpleTestCase):
    @mock.patch.object(V, "_public_hosts", return_value=set())
    @mock.patch.object(V, "matching_rule", return_value=None)
    def test_ok_returns_normalized(self, _mr, _ph):
        self.assertEqual(V.validate_tenant_domain(" Acme.Client.com "), "acme.client.com")

    @mock.patch.object(V, "matching_rule", return_value=None)
    @mock.patch.object(V, "_public_hosts", return_value={"manage.routegenie.com"})
    def test_public_host_reserved(self, _ph, _mr):
        with self.assertRaisesMessage(ValidationError, "reserved for platform management"):
            V.validate_tenant_domain("manage.routegenie.com")

    @mock.patch.object(V, "_public_hosts", return_value=set())
    def test_rule_denies(self, _ph):
        with mock.patch.object(V, "matching_rule", return_value=rule(R.MatchType.LABEL, "www")):
            with self.assertRaisesMessage(ValidationError, "Subdomain 'www' is reserved"):
                V.validate_tenant_domain("www.client.com")

    def test_bad_format_rejected_before_db(self):
        # Malformed host fails on format — no DB helper should be consulted.
        with mock.patch.object(V, "_public_hosts", side_effect=AssertionError("should not query")):
            with self.assertRaises(ValidationError):
                V.validate_tenant_domain("bad_underscore.com")


class CandidateSupersetContractTests(SimpleTestCase):
    """Guards the SUPERSET contract of candidate_q() semantically (DB-free): every host
    matches() accepts MUST be reachable by candidate_q()'s shape, otherwise the hybrid
    conflicts scan would silently drop a real conflict. Uses a faithful Python mirror of
    candidate_q()'s case-insensitive lookups; the real Q runs against the DB harness.
    """

    HOSTS = [
        "www.acme.com", "WWW.Mixed.com", "api.acme.com", "acme.routegenie.com",
        "admin.routegenie.com", "admin.client.com", "internal.x.com",
        "x.internal.x.com", "manage.localhost", "routegenie.com", "alpha.company1.com",
    ]

    def _prefilter(self, r, host):
        """Mirror of candidate_q()'s SQL semantics (case-insensitive)."""
        h = host.strip().lower().rstrip(".")
        val = V.normalize_host(r.value)
        if r.match_type == R.MatchType.EXACT:
            return h == val
        if r.match_type == R.MatchType.SUFFIX:
            return h == val or h.endswith("." + val)
        if r.match_type == R.MatchType.LABEL:
            return h == val or h.startswith(val + ".")
        return False

    def test_matches_is_subset_of_prefilter(self):
        rules = [
            rule(R.MatchType.LABEL, "www"),
            rule(R.MatchType.LABEL, "admin", base="routegenie.com"),
            rule(R.MatchType.EXACT, "manage.localhost"),
            rule(R.MatchType.SUFFIX, "internal.x.com"),
        ]
        for r in rules:
            for h in self.HOSTS:
                if r.matches(h):
                    self.assertTrue(
                        self._prefilter(r, h),
                        f"{r}: matches({h!r}) but candidate_q shape excludes it — SUPERSET broken",
                    )


class ValidateSchemaNameGlueTests(SimpleTestCase):
    @mock.patch.object(V, "reserved_schema_labels", return_value={"admin", "api", "www"})
    def test_reserved_rejected(self, _rl):
        with self.assertRaisesMessage(ValidationError, "reserved"):
            V.validate_tenant_schema_name("admin")

    @mock.patch.object(V, "reserved_schema_labels", return_value={"admin", "api"})
    def test_allowed_passes(self, _rl):
        self.assertEqual(V.validate_tenant_schema_name("acme"), "acme")


class BootstrapTenantReservedCheckTests(SimpleTestCase):
    """bootstrap_tenant._check_reserved: enforce reserved rules on the CLI path, with a
    --force escape hatch. Validators are mocked, so this is DB-free."""

    def _cmd(self):
        out = io.StringIO()
        cmd = bt_mod.Command(stdout=out)
        cmd._captured = out
        return cmd

    def test_blocks_without_force(self):
        with mock.patch.object(bt_mod, "validate_tenant_schema_name",
                               side_effect=ValidationError("Schema name 'admin' is reserved.")), \
             mock.patch.object(bt_mod, "validate_tenant_domain", return_value="x"):
            cmd = self._cmd()
            with self.assertRaisesMessage(CommandError, "Use --force to override"):
                cmd._check_reserved("admin", "admin.foo.com", force=False)

    def test_force_warns_and_proceeds(self):
        with mock.patch.object(bt_mod, "validate_tenant_schema_name",
                               side_effect=ValidationError("reserved")), \
             mock.patch.object(bt_mod, "validate_tenant_domain", return_value="x"):
            cmd = self._cmd()
            self.assertIsNone(cmd._check_reserved("admin", "admin.foo.com", force=True))
            self.assertIn("overriding reserved", cmd._captured.getvalue())

    def test_passes_when_valid(self):
        with mock.patch.object(bt_mod, "validate_tenant_schema_name", return_value="acme"), \
             mock.patch.object(bt_mod, "validate_tenant_domain", return_value="acme.foo.com"):
            cmd = self._cmd()
            self.assertIsNone(cmd._check_reserved("acme", "acme.foo.com", force=False))