from django.db import connection
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import User


def _on_tenant(request) -> bool:
    """True iff this request is being served by a tenant schema (not public)."""
    return connection.schema_name != "public"


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