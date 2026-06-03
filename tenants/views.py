from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connections
from django.db.models import F
from django.http import HttpResponse
from django_tenants.utils import schema_context
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from users.models import User

from .models import Shard, Tenant
from .permissions import IsTenantAdminOnPublic
from .serializers import ShardSerializer, TenantSerializer


def health(request):
    """Liveness probe for ALB target group health checks.

    Plain HTTP 200 without touching the database. The nginx proxies this
    through to gunicorn, so a healthy response means both nginx and the
    Django ASGI worker are responsive.
    """
    return HttpResponse("ok", content_type="text/plain")


class ShardViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only list of shards available for tenant placement.

    Excludes the default shard (reserved for the public schema) and inactive
    shards. The frontend uses this to populate the shard dropdown in the
    tenant creation form.
    """

    queryset = Shard.objects.filter(is_active=True, is_default=False).order_by("alias")
    serializer_class = ShardSerializer
    permission_classes = [IsTenantAdminOnPublic]


class TenantViewSet(viewsets.ModelViewSet):
    """CRUD over tenants. Reachable only on the public host."""

    queryset = (
        Tenant.objects.exclude(schema_name="public")
        .select_related("shard")
        .order_by("-created_on")
    )
    serializer_class = TenantSerializer
    permission_classes = [IsTenantAdminOnPublic]

    # -------------------------------------------------------------------
    # schema_exists pre-fetch
    # -------------------------------------------------------------------

    def get_serializer_context(self):
        """For list/retrieve, pre-compute which tenant schemas physically
        exist by issuing one SELECT against information_schema.schemata per
        shard. This avoids N+1 in TenantSerializer.get_schema_exists.
        """
        ctx = super().get_serializer_context()
        if self.action in ("list", "retrieve", "deactivate", "activate"):
            ctx["existing_schemas"] = self._existing_schemas_for(self.get_queryset())
        return ctx

    @staticmethod
    def _existing_schemas_for(qs) -> set:
        """Return a set of (shard_alias, schema_name) tuples that actually
        exist on each shard. One query per shard.
        """
        by_shard: dict[str, set[str]] = {}
        for t in qs:
            by_shard.setdefault(t.shard.alias, set()).add(t.schema_name)

        result: set = set()
        for alias, schemas in by_shard.items():
            with connections[alias].cursor() as cur:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name = ANY(%s)",
                    [list(schemas)],
                )
                for (s,) in cur.fetchall():
                    result.add((alias, s))
        return result

    # -------------------------------------------------------------------
    # Custom actions
    # -------------------------------------------------------------------

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

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """Transition ACTIVE -> DEACTIVATED via atomic UPDATE WHERE.

        The serializer's status field is read-only, so this dedicated action
        is the only way to flip the bit from the API. We use UPDATE WHERE to
        avoid races with a concurrent migrate_schemas / reconcile_tenants run.
        """
        return self._transition(
            pk,
            from_status=Tenant.Status.ACTIVE,
            to_status=Tenant.Status.DEACTIVATED,
        )

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Transition DEACTIVATED -> ACTIVE via atomic UPDATE WHERE."""
        return self._transition(
            pk,
            from_status=Tenant.Status.DEACTIVATED,
            to_status=Tenant.Status.ACTIVE,
        )

    def _transition(self, pk, *, from_status: str, to_status: str):
        tenant = self.get_object()
        updated = Tenant.objects.filter(pk=tenant.pk, status=from_status).update(
            previous_status=F("status"),
            status=to_status,
        )
        if not updated:
            return Response(
                {
                    "detail": (
                        f"Cannot transition tenant '{tenant.schema_name}' from "
                        f"'{tenant.status}' to '{to_status}'. Expected current status "
                        f"to be '{from_status}'."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        tenant.refresh_from_db()
        serializer = self.get_serializer(tenant)
        return Response(serializer.data)
