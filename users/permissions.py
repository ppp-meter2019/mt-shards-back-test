from django.conf import settings
from django.db import connection
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import User


def _on_tenant(request) -> bool:
    """True iff this request may access tenant business data.

    MT: the request must be served by a tenant schema (not public).
    Standalone: there is no public/tenant split — the single DB *is* the tenant —
    so this is always satisfied. (connection.schema_name is injected by the
    django_tenants backend and does NOT exist on the plain PostGIS backend, hence
    the getattr guard rather than a bare attribute read.)
    """
    if not settings.USE_MULTITENANT:
        return True
    # Literal "public", not get_public_schema_name(): this module must import cleanly in
    # STANDALONE, where django_tenants is NOT installed (audited standalone-safe file —
    # see the ALLOW list in scripts/ci_guard_schema_name.sh).
    return getattr(connection, "schema_name", "public") != "public"


class IsCompanyAdmin(BasePermission):
    """Full read/write inside a tenant schema."""

    def has_permission(self, request, view) -> bool:
        u = request.user
        return (
            bool(u and u.is_authenticated)
            and _on_tenant(request)
            and u.role == User.Role.COMPANY_ADMIN
        )


class IsCustomer(BasePermission):
    def has_permission(self, request, view) -> bool:
        u = request.user
        return (
            bool(u and u.is_authenticated)
            and _on_tenant(request)
            and u.role == User.Role.CUSTOMER
        )


class IsDriver(BasePermission):
    def has_permission(self, request, view) -> bool:
        u = request.user
        return (
            bool(u and u.is_authenticated)
            and _on_tenant(request)
            and u.role == User.Role.DRIVER
        )


class IsCompanyAdminOrReadOnly(BasePermission):
    """Anyone authenticated on a tenant can read; only admin can mutate.
    Used for catalog endpoints (products) where customers need to browse."""

    def has_permission(self, request, view) -> bool:
        u = request.user
        if not (u and u.is_authenticated and _on_tenant(request)):
            return False
        if request.method in SAFE_METHODS:
            return True
        return u.role == User.Role.COMPANY_ADMIN


class IsCompanyAdminOrCustomer(BasePermission):
    """Used on /api/orders/: customers manage their own orders, admins
    manage everyone's. Object-level ownership is enforced separately."""

    def has_permission(self, request, view) -> bool:
        u = request.user
        return (
            bool(u and u.is_authenticated)
            and _on_tenant(request)
            and u.role in {User.Role.COMPANY_ADMIN, User.Role.CUSTOMER}
        )