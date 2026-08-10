import logging
import re

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import Error as DBError, connections
from django.db.models import Count, F
from django.db.models.deletion import ProtectedError
from django.db.utils import ConnectionDoesNotExist
from django.http import HttpResponse
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from django_tenants.utils import get_public_schema_name
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import User

from .context import tenant_context
from .models import Domain, ReservedHostRule, Shard, Tenant
from .permissions import IsTenantAdminOnPublic
from .resolve_cache import resolve_cache
from .serializers import (
    ReservedHostRuleSerializer,
    ShardSerializer,
    TenantSerializer,
)

# Defence-in-depth: schema names are already validated on creation, but they're
# interpolated as SQL identifiers in _last_migrations_for, so re-check here.
_SAFE_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

logger = logging.getLogger(__name__)


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


class BaseDomainsView(APIView):
    """Read-only list of platform base domains (settings.TENANT_BASE_DOMAINS) for the
    tenant/domain create+edit form's base-domain dropdown. Public host, tenant_admin
    only. The dropdown adds its own "custom domain" option client-side."""

    permission_classes = [IsTenantAdminOnPublic]

    def get(self, request):
        return Response({"base_domains": list(getattr(settings, "TENANT_BASE_DOMAINS", ()))})


def _psql_aligned(headers, rows, title):
    """Render rows as psql's aligned table (centered title + headers, '+' line
    continuation for multi-line cells, '(N rows)' footer). Cosmetic — to make
    the API response read like real \\dn+ console output.
    """
    ncols = len(headers)
    widths = [len(h) for h in headers]
    grid = []
    for row in rows:
        cells = []
        for i in range(ncols):
            lines = ("" if row[i] is None else str(row[i])).split("\n")
            cells.append(lines)
            for ln in lines:
                widths[i] = max(widths[i], len(ln))
        grid.append(cells)

    def hcell(text, w):                       # header: centered, 1 space padding
        pad = w - len(text)
        return " " + " " * (pad // 2) + text + " " * (pad - pad // 2) + " "

    def dcell(text, w, cont):                 # data: left-aligned; '+' if continued
        return " " + text.ljust(w) + ("+" if cont else " ")

    header = "|".join(hcell(headers[i], widths[i]) for i in range(ncols))
    sep = "+".join("-" * (widths[i] + 2) for i in range(ncols))
    out = [(" " * max(0, (len(header) - len(title)) // 2)) + title, header, sep]
    for cells in grid:
        nsub = max(len(c) for c in cells)
        for sub in range(nsub):
            out.append("|".join(
                dcell(cells[i][sub] if sub < len(cells[i]) else "",
                      widths[i], sub < len(cells[i]) - 1)
                for i in range(ncols)
            ))
    out.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(out)


class ShardViewSet(mixins.ListModelMixin,
                   mixins.RetrieveModelMixin,
                   mixins.DestroyModelMixin,
                   viewsets.GenericViewSet):
    """Shard management, reachable only on the public host.

    Lists ALL shards (with tenant_count + timestamps) and supports
    activate / deactivate / delete under strict rules. The tenant create-form
    filters this list client-side to active, non-default shards.

    Rules:
      - the default shard is READ-ONLY (no activate/deactivate/delete);
      - activate:   only a deactivated shard;
      - deactivate: only a shard with zero tenants;
      - delete:     only a deactivated shard.
    """

    queryset = (
        Shard.objects.annotate(tenant_count=Count("tenants"))
                     .order_by("-is_default", "alias")
    )
    serializer_class = ShardSerializer
    permission_classes = [IsTenantAdminOnPublic]

    @staticmethod
    def _guard_default(shard):
        if shard.is_default:
            raise PermissionDenied("The default shard is read-only.")

    @action(detail=True, methods=["get"])
    def schemas(self, request, pk=None):
        """Low-level peek: schemas on this shard's DB, rendered like psql `\\dn+`.

        Read-only, fixed catalog query on the shard's own connection (no user
        input → no injection). tenant_admin-only via the viewset perms. Returns
        a single console-style `output` string for display in a <pre>.
        """
        shard = self.get_object()
        with connections[shard.alias].cursor() as cur:
            cur.execute(
                "SELECT n.nspname, "
                "       pg_catalog.pg_get_userbyid(n.nspowner), "
                "       pg_catalog.array_to_string(n.nspacl, E'\\n'), "
                "       pg_catalog.obj_description(n.oid, 'pg_namespace') "
                "FROM pg_catalog.pg_namespace n "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                "ORDER BY 1"
            )
            rows = [[r[0], r[1], r[2] or "", r[3] or ""] for r in cur.fetchall()]

        dbname = settings.DATABASES[shard.alias]["NAME"]
        table = _psql_aligned(
            ["Name", "Owner", "Access privileges", "Description"], rows, "List of schemas"
        )
        output = f"{dbname}=> \\dn+\n{table}"
        return Response({"shard": shard.alias, "output": output})

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Activate a deactivated shard."""
        shard = self.get_object()
        self._guard_default(shard)
        if shard.is_active:
            return Response({"detail": "Shard is already active."},
                            status=status.HTTP_409_CONFLICT)
        shard.is_active = True
        shard.save(update_fields=["is_active", "modified"])
        return Response(self.get_serializer(shard).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """Deactivate a shard that hosts no tenants."""
        shard = self.get_object()
        self._guard_default(shard)
        if not shard.is_active:
            return Response({"detail": "Shard is already deactivated."},
                            status=status.HTTP_409_CONFLICT)
        tenant_count = shard.tenants.count()
        if tenant_count:
            return Response(
                {"detail": f"Cannot deactivate: shard hosts {tenant_count} "
                           f"tenant(s). Move or delete them first."},
                status=status.HTTP_409_CONFLICT,
            )
        shard.is_active = False
        shard.save(update_fields=["is_active", "modified"])
        return Response(self.get_serializer(shard).data)

    def destroy(self, request, *args, **kwargs):
        """Delete a deactivated shard (default shard / active shard rejected)."""
        shard = self.get_object()
        self._guard_default(shard)
        if shard.is_active:
            return Response(
                {"detail": "Can only delete a deactivated shard. Deactivate it first."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            shard.delete()
        except ProtectedError:
            return Response(
                {"detail": "Shard cannot be deleted - it still has tenants."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


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
        if tenant.schema_name == get_public_schema_name():
            raise PermissionDenied("The public tenant is read-only.")

    def perform_update(self, serializer):
        self._guard_public(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._guard_public(instance)
        # Optional: also drop the tenant's schema (DELETE ?drop_schema=true).
        # Deleting the row leaves the schema (auto_drop_schema=False); when the
        # operator opts in we queue a service-queue task to drop it on the shard.
        # Capture shard+schema BEFORE delete (the instance is gone afterwards).
        drop = str(self.request.query_params.get("drop_schema", "")).lower() in (
            "1", "true", "yes", "on",
        )
        alias, schema = instance.shard.alias, instance.schema_name
        instance.delete()
        if drop:
            from .tasks import drop_tenant_schema_task
            drop_tenant_schema_task.delay(alias, schema)

    # -------------------------------------------------------------------
    # schema_exists pre-fetch
    # -------------------------------------------------------------------

    def get_serializer_context(self):
        """Pre-compute schema_exists/last_migration (physical state) so the serializer
        doesn't N+1.

        Scope by action:
          - list                         -> all tenants (that IS the console's job);
          - retrieve/activate/deactivate -> ONLY the target tenant, so a single-object
                                            response scans just its shard, not every
                                            shard (avoids waste + a cross-shard failure).
          - create/update/partial_update -> nothing (schema irrelevant / FE ignores it).

        The probes themselves degrade per shard (see the helpers): a down shard never
        500s the whole page.
        """
        ctx = super().get_serializer_context()
        if self.action == "list":
            qs = self.get_queryset()
        elif self.action in ("retrieve", "deactivate", "activate"):
            # Single-object response → scope to just this tenant (its shard only).
            # Mirror DRF's get_object() lookup so it survives a custom lookup_field.
            lookup = self.lookup_url_kwarg or self.lookup_field
            qs = self.get_queryset().filter(**{self.lookup_field: self.kwargs.get(lookup)})
        else:
            return ctx                                  # create/update: no physical scan
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
            # Degrade per shard: a down/unreachable shard must not 500 the whole
            # tenants console (a diagnostic tool). Its tenants fall back to
            # schema_exists=False ("not confirmed") and are logged.
            try:
                with connections[alias].cursor() as cur:
                    cur.execute(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = ANY(%s)",
                        [list(schemas)],
                    )
                    for (s,) in cur.fetchall():
                        result.add((alias, s))
            except (DBError, ConnectionDoesNotExist):
                # Degrade only on DB/connection failures (shard down, alias not in
                # DATABASES). A programming error is NOT swallowed — let it 500 so
                # it's noticed, not silently hidden behind an empty result.
                logger.warning("schema_exists probe failed for shard %r; its tenants "
                               "shown as unconfirmed", alias, exc_info=True)
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
            # Degrade per shard, like _existing_schemas_for: an unreachable shard is
            # skipped (its tenants show last_migration=None), never 500s the console.
            try:
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
            except (DBError, ConnectionDoesNotExist):
                # Degrade only on DB/connection failures — see _existing_schemas_for.
                logger.warning("last_migration probe failed for shard %r; its tenants "
                               "shown without a last migration", alias, exc_info=True)
        return result

    # -------------------------------------------------------------------
    # Custom actions
    # -------------------------------------------------------------------

    @action(detail=True, methods=["post"])
    def provision(self, request, pk=None):
        """Queue async provisioning (create schema + migrate) for a NEW tenant.

        Enqueues provision_tenant on the `slow` queue; the worker does the
        atomic NEW->PENDING claim, CREATE SCHEMA, migrate, NEW->ACTIVE/FAILED.

        Re-provisioning guard: only a NEW tenant is provisionable. Any other
        status is rejected (409) — you cannot re-provision an already-provisioned
        tenant (ACTIVE/DEACTIVATED), one in progress (PENDING), or a FAILED one
        (reset it via reconcile_tenants first).
        """
        tenant = self.get_object()
        self._guard_public(tenant)
        if tenant.status != Tenant.Status.NEW:
            return Response(
                {
                    "detail": (
                        f"Tenant '{tenant.schema_name}' is not provisionable "
                        f"(status '{tenant.status}'). Provisioning runs only on a "
                        f"NEW tenant."
                    ),
                    "code": "not_provisionable",
                },
                status=status.HTTP_409_CONFLICT,
            )
        from .tasks import provision_tenant
        provision_tenant.delay(tenant.id)
        return Response(
            {"detail": "Provisioning queued.", "schema": tenant.schema_name},
            status=status.HTTP_202_ACCEPTED,
        )

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
        resolve_cache.forget_tenant(tenant)   # .update() bypasses post_save; drop cached snapshot(s)
        tenant.refresh_from_db()
        serializer = self.get_serializer(tenant)
        return Response(serializer.data)


class ReservedHostRuleViewSet(viewsets.ModelViewSet):
    """CRUD over reserved-host rules, reachable only on the public host.

    These rules forbid business tenants from claiming service subdomains
    (www/api/admin/...) and platform hosts. Enforced on tenant/domain creation by
    tenants.validators.validate_tenant_domain.
    """

    queryset = ReservedHostRule.objects.all()          # Meta.ordering handles order
    serializer_class = ReservedHostRuleSerializer
    permission_classes = [IsTenantAdminOnPublic]

    # How many example domains to include in the response body. `count` is ALWAYS
    # exact (we stream the full candidate set); this only bounds the payload, so a
    # pathological rule can't return megabytes. `sample=true` tells the caller the
    # `domains` list is a subset of `count`.
    CONFLICTS_SAMPLE = 200

    @action(detail=True, methods=["get"])
    def conflicts(self, request, pk=None):
        """Report EXISTING (non-public) tenant domains this rule already reserves.

        Lets an operator see, before relying on a rule, which live hosts it would
        have blocked — a rule only gates FUTURE creations, so pre-existing matches
        keep working and are surfaced here rather than silently ignored.

        Hybrid: candidate_q() narrows to a SUPERSET in SQL, then matches() (the one
        matcher) confirms — DB does the bulk work without re-implementing matching.
        The full candidate set is STREAMED (memory bounded by the iterator), so
        `count` is exact with no completeness cap; only the example list is bounded.
        """
        rule = self.get_object()
        qs = (
            Domain.objects.select_related("tenant")
            .exclude(tenant__schema_name=get_public_schema_name())
            .filter(rule.candidate_q())
            .order_by("domain")
        )
        count, sample = 0, []
        for d in qs.iterator(chunk_size=1000):
            if rule.matches(d.domain):
                count += 1
                if len(sample) < self.CONFLICTS_SAMPLE:
                    sample.append({"domain": d.domain, "tenant": d.tenant.schema_name})
        return Response({"count": count, "domains": sample, "sample": len(sample) < count})
