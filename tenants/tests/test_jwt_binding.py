"""Schema-bound JWT authentication — tenants/auth/jwt.py. DB-free (no token minting, no HTTP).

The twin of test_auth_binding.py: both files close the SAME invariant — a credential issued
on schema X must not work on schema Y — one for cookie sessions, one for bearer tokens. The
session side arrived here with its tests; this side arrived without any, so deleting the
check in jwt.py left all 428 tests green.

Nothing here needs a real token: get_validated_token() only calls `.get("schema")` on whatever
the parent returned, so a plain dict stands in for it and the parent is patched out.
"""
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from tenants.auth.jwt import SchemaBoundJWTAuthentication


class _CountingConnection:
    """Stands in for django.db.connection and records every schema_name read, so a test can
    assert the check did NOT run (rather than only that it produced the right answer)."""

    def __init__(self, schema_name: str) -> None:
        self._schema_name = schema_name
        self.reads = 0

    @property
    def schema_name(self) -> str:
        self.reads += 1
        return self._schema_name


class SchemaBoundJWTTests(SimpleTestCase):
    def _authenticate(self, claims, served_schema="alpha", parent_raises=None):
        """Run get_validated_token() with the parent stubbed and the served schema fixed.

        Returns (result, connection) so a caller can inspect the read counter.
        """
        conn = _CountingConnection(served_schema)
        parent = (mock.Mock(side_effect=parent_raises) if parent_raises
                  else mock.Mock(return_value=claims))
        with mock.patch("tenants.auth.jwt.connection", conn), \
             mock.patch.object(JWTAuthentication, "get_validated_token", parent):
            return SchemaBoundJWTAuthentication().get_validated_token(b"raw"), conn

    def test_same_schema_passes_the_token_through(self) -> None:
        claims = {"schema": "alpha", "user_id": 5}
        token, conn = self._authenticate(claims, served_schema="alpha")
        self.assertIs(token, claims)        # returned unchanged, not rebuilt
        self.assertEqual(conn.reads, 1)

    def test_cross_tenant_rejected(self) -> None:
        """user_id=5 exists in every schema; only the claim tells the two apart."""
        with self.assertRaises(InvalidToken):
            self._authenticate({"schema": "beta", "user_id": 5}, served_schema="alpha")

    def test_missing_schema_claim_is_fail_closed(self) -> None:
        """A token minted before the claim existed, or by a login path that skipped it."""
        with self.assertRaises(InvalidToken):
            self._authenticate({"user_id": 5}, served_schema="alpha")

    def test_null_schema_claim_is_fail_closed(self) -> None:
        with self.assertRaises(InvalidToken):
            self._authenticate({"schema": None, "user_id": 5}, served_schema="alpha")

    def test_public_schema_is_not_special_cased(self) -> None:
        """No bypass for the management host: a tenant token stays invalid there."""
        with self.assertRaises(InvalidToken):
            self._authenticate({"schema": "alpha"}, served_schema="public")
        token, _ = self._authenticate({"schema": "public"}, served_schema="public")
        self.assertEqual(token["schema"], "public")

    def test_signature_is_verified_before_the_claim(self) -> None:
        """Order matters: our check must NOT run on a token the parent already rejected.

        If it ran first, a bad signature with the right schema and a good signature with the
        wrong one would raise different exceptions — an oracle for enumerating schema names.
        """
        conn = _CountingConnection("alpha")
        boom = TokenError("bad signature")
        with mock.patch("tenants.auth.jwt.connection", conn), \
             mock.patch.object(JWTAuthentication, "get_validated_token",
                               mock.Mock(side_effect=boom)):
            with self.assertRaises(TokenError) as ctx:
                SchemaBoundJWTAuthentication().get_validated_token(b"raw")
        self.assertIs(ctx.exception, boom)          # the parent's error, not ours
        self.assertNotIsInstance(ctx.exception, InvalidToken)
        self.assertEqual(conn.reads, 0)             # the claim check never ran

    def test_it_is_the_wired_authentication_class(self) -> None:
        """Catches a merge that repoints DRF back at stock JWTAuthentication."""
        self.assertIn(
            "tenants.auth.jwt.SchemaBoundJWTAuthentication",
            settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"],
        )
