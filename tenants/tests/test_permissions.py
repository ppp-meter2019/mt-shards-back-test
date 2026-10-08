"""IsTenantAdminOnPublic — tenants/permissions.py. DB-free (no User rows, no HTTP).

has_permission() is called directly with a stand-in request: it reads exactly three things
(`user.is_authenticated`, `connection.schema_name`, `user.role`), none of which needs a
database. The DB-backed variant in db_integration.py covers the DRF plumbing, but that module
is deliberately undiscoverable and runs nowhere in CI, so these are the only tests that gate
the four management endpoints.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase
from django_tenants.utils import get_public_schema_name

from tenants.console.views import (
    BaseDomainsView,
    ReservedHostRuleViewSet,
    ShardViewSet,
    TenantViewSet,
)
from tenants.permissions import IsTenantAdminOnPublic


class _CountingConnection:
    """Records every schema_name read, so a test can assert the check short-circuited."""

    def __init__(self, schema_name: str) -> None:
        self._schema_name = schema_name
        self.reads = 0

    @property
    def schema_name(self) -> str:
        self.reads += 1
        return self._schema_name


class _Request:
    def __init__(self, user) -> None:
        self.user = user


class _User:
    is_authenticated = True

    def __init__(self, role: str) -> None:
        self.role = role


class IsTenantAdminOnPublicTests(SimpleTestCase):
    PUBLIC = get_public_schema_name()

    def setUp(self) -> None:
        self.perm = IsTenantAdminOnPublic()
        Role = get_user_model().Role
        self.admin = _User(Role.TENANT_ADMIN)
        self.company = _User(Role.COMPANY_ADMIN)

    def _check(self, user, schema):
        conn = _CountingConnection(schema)
        with mock.patch("tenants.permissions.connection", conn):
            return self.perm.has_permission(_Request(user), None), conn

    def test_tenant_admin_on_public_is_allowed(self) -> None:
        allowed, _ = self._check(self.admin, self.PUBLIC)
        self.assertIs(allowed, True)

    def test_wrong_role_on_public_is_denied(self) -> None:
        """The role check is the one that keeps a public-schema login from managing tenants."""
        allowed, _ = self._check(self.company, self.PUBLIC)
        self.assertIs(allowed, False)

    def test_tenant_admin_on_a_tenant_schema_is_denied(self) -> None:
        """Second line of defence behind PUBLIC_SCHEMA_URLCONF — see the class docstring."""
        allowed, _ = self._check(self.admin, "alpha")
        self.assertIs(allowed, False)

    def test_wrong_role_on_a_tenant_schema_is_denied(self) -> None:
        allowed, _ = self._check(self.company, "alpha")
        self.assertIs(allowed, False)

    def test_anonymous_is_denied_without_reading_the_role(self) -> None:
        """AnonymousUser has no `.role`; reaching it would raise AttributeError -> 500.

        So the authentication check must come FIRST and short-circuit — asserted here by the
        read counter, not just by the return value.
        """
        allowed, conn = self._check(AnonymousUser(), self.PUBLIC)
        self.assertIs(allowed, False)
        self.assertEqual(conn.reads, 0)

    def test_missing_user_is_denied(self) -> None:
        """request.user is None when no authentication class ran at all."""
        allowed, conn = self._check(None, self.PUBLIC)
        self.assertIs(allowed, False)
        self.assertEqual(conn.reads, 0)

    def test_every_management_endpoint_is_gated(self) -> None:
        """Catches a merge that drops the permission, or lands DRF's AllowAny default."""
        for view in (BaseDomainsView, ShardViewSet, TenantViewSet, ReservedHostRuleViewSet):
            with self.subTest(view=view.__name__):
                self.assertEqual(list(view.permission_classes), [IsTenantAdminOnPublic])

    def test_denial_carries_a_message(self) -> None:
        self.assertTrue(str(IsTenantAdminOnPublic.message).strip())
