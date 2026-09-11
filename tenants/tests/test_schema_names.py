"""Schema-name convention + SQL-identifier safety (tenants.validators).

Three functions, three contracts, and the GAP between them is deliberate:

  validate_schema_name()      the CONVENTION for names we create (normalizes, then raises)
  is_safe_schema_identifier() the SAFETY floor for names that may predate it (boolean)
  quote_schema()              validate AND quote together, so no call site can do one only

Why three functions and not one shared regex: a single pattern would have to answer both
"may we create this name?" and "is this safe inside SQL?", which are different questions with
different answers (a leading digit is fine for both; `Foo-Bar` is safe in SQL but not
canonical). The ADMIN path needs the first one explicitly — django-tenants' own model-field
validator is `^(?!pg_).{1,63}$`, i.e. nearly anything. These tests pin all of that.
"""
from typing import TYPE_CHECKING
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

if TYPE_CHECKING:                       # annotation-only: the model import stays inside
    from tenants.models import Tenant   # _tenant(), where it is deliberately lazy

from tenants.validators import (
    SCHEMA_NAME_MAX,
    is_safe_schema_identifier,
    normalize_schema_name,
    quote_schema,
    validate_schema_name,
)


class NormalizeSchemaNameTests(SimpleTestCase):
    """Upper case and hyphens are FOLDED, not rejected: both are what a human types when
    mirroring a host label, and a host label legitimately allows them while a schema name
    does not. So host-label -> schema_name is not the identity."""

    def test_folds_case_hyphens_and_whitespace(self) -> None:
        for raw, want in [
            ("acme", "acme"),
            ("  acme  ", "acme"),
            ("Foo-Bar", "foo_bar"),
            ("Freedom-First", "freedom_first"),
            ("1ST-CHOICE", "1st_choice"),
            ("24-7-transit", "24_7_transit"),          # the host-label mapping case
            ("a-b-c", "a_b_c"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_schema_name(raw), want)

    def test_none_and_empty_are_the_empty_string(self) -> None:
        self.assertEqual(normalize_schema_name(None), "")
        self.assertEqual(normalize_schema_name("   "), "")


class ValidateSchemaNameTests(SimpleTestCase):

    def test_accepts_a_leading_digit(self) -> None:
        """'1st_choice' is a legitimate company name. Postgres needs such a schema quoted,
        which is why every SQL site goes through quote_schema() instead of hand-quoting."""
        self.assertEqual(validate_schema_name("1st_choice"), "1st_choice")
        self.assertEqual(validate_schema_name("123"), "123")

    def test_returns_the_normalized_name(self) -> None:
        """Callers MUST use the return value — the input is not what gets stored."""
        self.assertEqual(validate_schema_name("Freedom-First"), "freedom_first")

    def test_rejects_what_cannot_be_normalized(self) -> None:
        for raw in [
            'a"b',                                   # breaks our CREATE/DROP SCHEMA "{s}"
            "o'brien",                               # breaks upstream SET search_path='{s}'
            'a";DROP SCHEMA public CASCADE;--',      # the case the DDL guards exist for
            "a b", "a.b", "../etc", "a/b",           # spaces, dots, path separators
            "нортсайд",                              # non-ASCII
            "_foo", "-foo",                          # leading underscore (folded from '-')
            "",  "   ", None,
        ]:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                validate_schema_name(raw)

    def test_rejects_pg_prefix_case_insensitively(self) -> None:
        """Folding to lower case makes the pg_ guard case-insensitive for free."""
        for raw in ["pg_x", "PG_X", "Pg_toast"]:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                validate_schema_name(raw)

    def test_length_boundary(self) -> None:
        self.assertEqual(len(validate_schema_name("a" * SCHEMA_NAME_MAX)), SCHEMA_NAME_MAX)
        with self.assertRaises(ValidationError):
            validate_schema_name("a" * (SCHEMA_NAME_MAX + 1))

    def test_over_length_error_explains_the_aliasing_risk(self) -> None:
        """PostgreSQL truncates past NAMEDATALEN-1, which would silently alias two
        tenants — the message must say so, not just 'too long'."""
        with self.assertRaises(ValidationError) as ctx:
            validate_schema_name("a" * 80)
        self.assertIn("truncates", ctx.exception.messages[0])


class IsSafeSchemaIdentifierTests(SimpleTestCase):
    """The floor is intentionally WIDER than the convention: it must not refuse a name that
    merely violates our naming preference, only one that can break out of quotes."""

    def test_permits_names_the_convention_rejects(self) -> None:
        for value in ["1st_choice", "Foo-Bar", "нортсайд", "_foo", "a b", "a.b"]:
            with self.subTest(value=value):
                self.assertTrue(is_safe_schema_identifier(value))

    def test_rejects_only_real_break_out_characters(self) -> None:
        for value in ['a"b', "o'brien", 'a";DROP SCHEMA public CASCADE;--',
                      "a\x00b", "a\nb", "a\tb", "", None, "a" * 64]:
            with self.subTest(value=value):
                self.assertFalse(is_safe_schema_identifier(value))

    def test_is_anchored_end_to_end(self) -> None:
        """views.py used `.match()` on an anchored pattern, so a trailing newline slipped
        through into a string that gets interpolated into SQL."""
        self.assertTrue(is_safe_schema_identifier("acme"))
        self.assertFalse(is_safe_schema_identifier("acme\n"))

    def test_does_not_normalize(self) -> None:
        """It answers a question about the string AS IT STANDS; normalizing here would make
        a name look safe that is not the name that reaches SQL."""
        self.assertTrue(is_safe_schema_identifier("Foo-Bar"))     # safe, but not canonical


class QuoteSchemaTests(SimpleTestCase):

    def test_quotes_valid_and_legacy_names(self) -> None:
        self.assertEqual(quote_schema("acme"), '"acme"')
        self.assertEqual(quote_schema("1st_choice"), '"1st_choice"')
        self.assertEqual(quote_schema("Foo-Bar"), '"Foo-Bar"')     # legacy still serviceable

    def test_raises_on_anything_that_could_break_out(self) -> None:
        """ValueError, not ValidationError: reaching here with such a value is a data/
        programming error, not user input — user input is rejected by validate_schema_name."""
        for value in ['a"b', "o'brien", 'a";DROP SCHEMA public CASCADE;--', "a\nb", "", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                quote_schema(value)

    def test_django_quote_name_alone_would_not_be_safe(self) -> None:
        """Why quote_schema exists rather than connection.ops.quote_name(): for PostgreSQL
        that is a bare '"%s"' % name with NO escaping of an internal `"`, so it cannot
        rescue an unvalidated name. Pinned so a future refactor does not 'simplify' to it."""
        from django.db import connections
        ops = connections["default"].ops
        self.assertEqual(ops.quote_name('a"b'), '"a"b"')          # broken SQL, no error
        with self.assertRaises(ValueError):
            quote_schema('a"b')                                    # ours refuses


class TenantCleanSchemaNameTests(SimpleTestCase):
    """Tenant.clean() is the ADMIN path, and the only format check on it: the model field
    itself carries django-tenants' `^(?!pg_).{1,63}$`, which accepts quotes and spaces. A
    schema_name accepted here reaches `CREATE SCHEMA "{...}"` in migrate_schemas, over
    psycopg's simple query protocol — which runs a second statement after a `"`.
    """

    def setUp(self) -> None:
        # Tenant.clean() also runs the RESERVED-LABEL check, which queries
        # ReservedHostRule — the one DB hit on this path. Stub just that lookup so the rest
        # of clean() (format validation + normalization, what these tests are about) runs
        # for real without a database. The reserved-label rules have their own coverage in
        # test_reserved_hosts.py and db_integration.py.
        patcher = mock.patch("tenants.validators.reserved_schema_labels", return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tenant(self, schema_name: str) -> "Tenant":
        from tenants.models import Shard, Tenant
        t = Tenant(schema_name=schema_name, company_name="X")
        t.shard = Shard(id=2, alias="shard_a", is_default=False, is_active=True)
        return t

    def test_rejects_an_injectable_schema_name(self) -> None:
        for raw in ['a";DROP SCHEMA public CASCADE;--', 'a"b', "o'brien", "a b", "нортсайд"]:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                self._tenant(raw).clean()

    def test_normalizes_on_create(self) -> None:
        """The admin form gets the canonical name written back, so what the operator typed
        and what lands in Postgres cannot diverge."""
        t = self._tenant("Freedom-First")
        t.clean()
        self.assertEqual(t.schema_name, "freedom_first")

    def test_accepts_a_leading_digit(self) -> None:
        t = self._tenant("1st_choice")
        t.clean()
        self.assertEqual(t.schema_name, "1st_choice")

    def test_does_not_revalidate_an_existing_row(self) -> None:
        """Create-only: schema_name is read-only on the admin change form, and revalidating
        would block editing a legacy row whose name predates the convention."""
        t = self._tenant("Foo-Bar")
        t.pk = 7                                    # existing row
        t.clean()                                   # must not raise
        self.assertEqual(t.schema_name, "Foo-Bar")  # and must not rewrite it
