from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView


class IsTenantAdminOnPublic(BasePermission):
    """Only tenant-administrators authenticated on the public schema may use this endpoint.

    The ROLE check is the live barrier: the public schema has its own auth_user, anyone who
    can log in there reaches this permission, and only `tenant_admin` may pass.

    The SCHEMA check is a second line of defence, not the first one. Every endpoint using this
    class is registered in tenants_back/urls_public.py, which django-tenants mounts as
    PUBLIC_SCHEMA_URLCONF; ROOT_URLCONF (urls_tenant.py) does not route them, so from a tenant
    host these URLs do not resolve at all and no permission runs. The check holds the invariant
    for the paths routing does not cover — a view mounted in both URLconfs, a direct call, a
    future management endpoint added to the tenant side — and it is what makes "manage tenants"
    impossible for a company-admin whose role was somehow set to 'tenant_admin' on a tenant DB.

    Order matters: authentication is checked FIRST because AnonymousUser has no `.role`, so
    reaching the last line with one would raise AttributeError (a 500, not a denial).
    See tenants/tests/test_permissions.py.
    """

    message = "Only tenant administrators on the management host may access this."

    def has_permission(self, request: Request, view: APIView) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        # get_public_schema_name(), not the literal "public": this module lives in the
        # `tenants` app, which is installed ONLY under multitenant, so django_tenants is
        # always importable here. The literal is reserved for the audited standalone-safe
        # readers outside this app (users/*, see scripts/ci_guard_schema_name.sh), where
        # importing django_tenants would break the standalone boot.
        if connection.schema_name != get_public_schema_name():
            return False
        # get_user_model(), not `from users.models import User`: AUTH_USER_MODEL is swappable,
        # and that module-level import was the only thing making `tenants` depend on `users` —
        # closing the cycle tenants -> users -> commons.platform -> tenants. The call must stay
        # INSIDE a function: at module level the app registry is not ready yet.
        return request.user.role == get_user_model().Role.TENANT_ADMIN
