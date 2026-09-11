from django.db import connection
from django_tenants.utils import get_public_schema_name
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
        # get_public_schema_name(), not the literal "public": this module lives in the
        # `tenants` app, which is installed ONLY under multitenant, so django_tenants is
        # always importable here. The literal is reserved for the audited standalone-safe
        # readers outside this app (users/*, see scripts/ci_guard_schema_name.sh), where
        # importing django_tenants would break the standalone boot.
        if connection.schema_name != get_public_schema_name():
            return False
        return request.user.role == User.Role.TENANT_ADMIN
