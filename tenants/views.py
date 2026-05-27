from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django_tenants.utils import schema_context
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from users.models import User

from .models import Tenant
from .permissions import IsTenantAdminOnPublic
from .serializers import TenantSerializer


class TenantViewSet(viewsets.ModelViewSet):
    """CRUD over tenants. Reachable only on the public host."""

    queryset = Tenant.objects.exclude(schema_name="public").order_by("-created_on")
    serializer_class = TenantSerializer
    permission_classes = [IsTenantAdminOnPublic]

    @action(detail=True, methods=["post"], url_path="create-admin")
    def create_admin(self, request, pk=None):
        """Bootstrap the first `company_admin` user inside the chosen tenant.

        Equivalent to `manage.py bootstrap_tenant --admin-username=... --admin-password=...`,
        but callable through the API by a logged-in tenant_admin. Useful right
        after creating a fresh tenant from the management UI — without this,
        the new tenant has no users and nobody can log into its admin.
        """
        tenant = self.get_object()
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""

        if not username:
            return Response(
                {"username": "This field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not password:
            return Response(
                {"password": "This field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(password)
        except DjangoValidationError as exc:
            return Response({"password": list(exc.messages)}, status=400)

        # Switch search_path to the tenant schema so the INSERT lands in
        # <schema>.users_user, not public.users_user.
        with schema_context(tenant.schema_name):
            if User.objects.filter(username=username).exists():
                return Response(
                    {"username": f"User '{username}' already exists in tenant '{tenant.schema_name}'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user = User.objects.create_user(
                username=username,
                password=password,
                role=User.Role.COMPANY_ADMIN,
                is_staff=True,
                is_superuser=True,
            )
            user_id = user.id

        return Response(
            {
                "id": user_id,
                "username": username,
                "tenant": tenant.schema_name,
                "role": User.Role.COMPANY_ADMIN,
            },
            status=status.HTTP_201_CREATED,
        )
