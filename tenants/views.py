import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connections
from django.db.models import F
from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from users.models import User

from .context import tenant_context
from .models import Shard, Tenant
from .permissions import IsTenantAdminOnPublic
from .serializers import ShardSerializer, TenantSerializer

# Defence-in-depth: schema names are already validated on creation, but they're
# interpolated as SQL identifiers in _last_migrations_for, so re-check here.
_SAFE_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def health(request):
    """Liveness probe for ALB target-group health checks.

    In practice this path is answered earlier by ShardAwareTenantMiddleware
    (see HEALTH_PATHS): as the outermost middleware it short-circuits with a
    plain 200 BEFORE host validation / tenant resolution, so the ALB's by-IP
    checks pass without tripping ALLOWED_HOSTS or "no tenant for hostname".
    This view is the registered fallback and returns the same plain 200 "ok";
    either way the probe touches neither the database nor the tenant.
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

    # The public tenant IS listed (so admins can see it), but it is read-only:
    # every write path below rejects it. It's a system record django-tenants
    # needs to route the public host.
    queryset = (
        Tenant.objects.select_related("shard").order_by("-created_on")
    )
    serializer_class = TenantSerializer
    permission_classes = [IsTenantAdminOnPublic]

    @staticmethod
    def _guard_public(tenant):
        """Reject any write targeting the public tenant."""
        if tenant.schema_name == "public":
            raise PermissionDenied("The public tenant is read-only.")

    def perform_update(self, serializer):
        self._guard_public(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._guard_public(instance)
        instance.delete()

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
            qs = self.get_queryset()
            ctx["existing_schemas"] = self._existing_schemas_for(qs)
            ctx["last_migrations"] = self._last_migrations_for(qs)
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

    @staticmethod
    def _last_migrations_for(qs) -> dict:
        """Return {(shard_alias, schema_name): {"app","name","applied"}} for the
        most recently applied migration in each tenant schema.

        Batched per shard (NOT per tenant): for each shard we run one query to
        find which target schemas actually have a django_migrations table, then
        one UNION over those schemas picking the latest row per schema via
        DISTINCT ON. So it's at most two queries per shard regardless of how many
        tenants live there.
        """
        by_shard: dict[str, set[str]] = {}
        for t in qs:
            by_shard.setdefault(t.shard.alias, set()).add(t.schema_name)

        result: dict = {}
        for alias, schemas in by_shard.items():
            schema_list = list(schemas)
            with connections[alias].cursor() as cur:
                # Which of these schemas actually have a django_migrations table?
                cur.execute(
                    "SELECT table_schema FROM information_schema.tables "
                    "WHERE table_name = 'django_migrations' AND table_schema = ANY(%s)",
                    [schema_list],
                )
                migrated = [s for (s,) in cur.fetchall() if _SAFE_SCHEMA_RE.match(s)]
                if not migrated:
                    continue
                # One UNION across the migrated schemas; latest row per schema.
                # Schema names are validated above, so safe to interpolate.
                union = " UNION ALL ".join(
                    f"SELECT '{s}' AS schema, id, app, name, applied "
                    f'FROM "{s}".django_migrations'
                    for s in migrated
                )
                cur.execute(
                    "SELECT DISTINCT ON (schema) schema, app, name, applied FROM ("
                    + union
                    + ") m ORDER BY schema, applied DESC NULLS LAST, id DESC"
                )
                for schema, app, name, applied in cur.fetchall():
                    result[(alias, schema)] = {
                        "app": app,
                        "name": name,
                        "applied": applied.isoformat() if applied else None,
                    }
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
        self._guard_public(tenant)
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

        # tenant_context wires both axes: routes the ORM to the tenant's shard
        # AND sets the schema on that shard's connection, so the INSERT lands
        # in <shard>.<schema>.users_user, not public.users_user.
        with tenant_context(tenant):
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
        self._guard_public(tenant)
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
