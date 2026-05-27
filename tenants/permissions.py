from django.db import connection
from rest_framework.permissions import BasePermission

from users.models import User


class IsTenantAdminOnPublic(BasePermission):
    """Only tenant-administrators authenticated on the public schema may use
    this endpoint. We check both the role and that we are actually on the
    public schema — a company-admin with role accidentally set to
    'tenant_admin' on a tenant DB shouldn't be able to manage tenants."""

    message = "Only tenant administrators on the management host may access this."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        if connection.schema_name != "public":
            return False
        return request.user.role == User.Role.TENANT_ADMIN
