"""Tenant + shard routing middleware (sync).

Two cooperating SYNC middlewares wire BOTH axes of multi-DB multi-tenancy (the schema
must be set on the same connection/thread the ORM later uses):

  A. ShardAwareTenantMiddleware - resolves the tenant from the Host (+ its shard in one
     query), sets request.tenant + the schema on the DEFAULT connection, gates on
     status, and delegates host->Tenant caching to tenants.resolver. The cache is
     an optimization: any cache-layer failure degrades to a plain DB resolve.

  B. TenantShardRoutingMiddleware - reads request.tenant, points the router at the
     shard (current_db) and sets/reset the tenant schema on the SHARD connection.
     Must be listed AFTER ShardAwareTenantMiddleware.
"""
import logging

import psycopg

from django.db import OperationalError, connections
from django.http import Http404, HttpResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name

from .context import current_db
from .errors import error_response
from .models import Tenant
from .resolver import resolve as resolve_tenant

logger = logging.getLogger(__name__)


class ShardAwareTenantMiddleware(TenantMainMiddleware):
    """TenantMainMiddleware that pulls the tenant's shard in the same query and gates
    on tenant status."""

    # Liveness answered HERE (outermost) before anything downstream: the ALB health-
    # checks by IP, so the Host is the instance IP — fails tenant resolution AND
    # ALLOWED_HOSTS. A static 200 skips both.
    HEALTH_PATHS = frozenset({"/api/health/"})

    def process_request(self, request):
        if request.path in self.HEALTH_PATHS:
            return HttpResponse("ok", content_type="text/plain")

        try:
            response = super().process_request(request)   # sets request.tenant + schema on `default`
        except Http404:
            # django-tenants raises Http404 for an unknown host (no Domain) — incl. our
            # cached-negative path. Serve a branded, negotiated 404 instead.
            logger.info("tenant not found: host=%r path=%s",
                        request.META.get("HTTP_HOST"), request.path)
            return error_response(
                request, status=404, code="tenant_not_found",
                detail="No workspace found for this address.",
                template="tenants/errors/not_found.html",
            )
        except (OperationalError, psycopg.OperationalError):
            # DB unreachable during resolution — branded 500 instead of a raw 500.
            # psycopg.OperationalError is caught too: django-tenants runs `SET search_path`
            # on a RAW psycopg cursor, so a pool/proxy borrow-timeout can escape UNWRAPPED
            # (not as django.db.OperationalError) from the schema-set step outside get_tenant.
            logger.error("tenant resolution DB error: path=%s", request.path, exc_info=True)
            return error_response(
                request, status=500, code="database_error",
                detail="A temporary error occurred. Please try again.",
                template="tenants/errors/database_error.html",
            )
        if response is not None:
            return response

        # Gate on tenant status — only ACTIVE business tenants may be served (their
        # whole host, API + admin). The PUBLIC tenant is EXEMPT: management lives on
        # the public host, which must stay reachable regardless of any status glitch.
        #   - DEACTIVATED -> 403 (intentionally closed — whole host incl. admin)
        #   - NEW/PENDING -> 503 (schema not provisioned/ready; the Domain exists from
        #     tenant creation, so without this the request would 500 at the DB layer)
        #   - FAILED      -> 503 for the API, but the tenant's OWN /admin/ stays reachable
        #     so an operator can log in and inspect (best-effort; a too-broken schema may
        #     still 500). Not a security downgrade: same auth as ACTIVE; unlike
        #     DEACTIVATED, FAILED is an operational (not closed) state.
        tenant = getattr(request, "tenant", None)
        if tenant is not None and tenant.schema_name != get_public_schema_name():
            st = tenant.status
            if st == Tenant.Status.DEACTIVATED:
                return error_response(
                    request, status=403, code="tenant_deactivated",
                    detail="This tenant is deactivated.",
                    template="tenants/errors/deactivated.html",
                )
            if st != Tenant.Status.ACTIVE:
                # `/admin` (no slash) counts too — APPEND_SLASH would redirect it to
                # `/admin/`, but CommonMiddleware runs after us, so match it here.
                is_admin = request.path == "/admin" or request.path.startswith("/admin/")
                if not (st == Tenant.Status.FAILED and is_admin):
                    return error_response(
                        request, status=503, code="tenant_not_ready",
                        detail="This tenant is not ready.",
                        template="tenants/errors/not_ready.html",
                        retry_after=300, extra={"status": st},
                    )
        return None

    def get_tenant(self, domain_model, hostname):
        # All resolve policy (cache, gate, fill_cap, coalescing, fail-open) lives in the
        # resolver service facade. The middleware only supplies the DB-resolver closure
        # (_resolve_tenant — the authoritative lookup) and the "not found" exception.
        return resolve_tenant(
            hostname,
            lambda: self._resolve_tenant(domain_model, hostname),
            domain_model.DoesNotExist,
        )

    @staticmethod
    def _resolve_tenant(domain_model, hostname):
        try:
            tenant = (
                domain_model.objects
                .select_related("tenant__shard")
                .get(domain=hostname)
                .tenant
            )
        except psycopg.OperationalError as exc:
            # django-tenants sets search_path on a RAW psycopg cursor, so a DB/pool error
            # (e.g. a pool / RDS-Proxy borrow-timeout: psycopg ConnectionException) escapes
            # UNWRAPPED here — NOT as django.db.OperationalError. Normalize it so get_tenant
            # surfaces it as a DB outage (branded 5xx via process_request) instead of
            # mislabeling it a cache failure and retrying a dead DB.
            raise OperationalError(str(exc)) from exc
        # request.tenant is a read-only routing snapshot, uniformly — whether resolved
        # fresh (here) or rebuilt from cache. Refuse save()/delete() either way so
        # behavior never depends on cache state (miss vs hit).
        tenant.read_only = True
        tenant.shard.read_only = True
        return tenant


class TenantShardRoutingMiddleware:
    """Routes the ORM to the tenant's shard and sets that shard connection's schema,
    resetting both on the way out."""

    sync_capable = True
    async_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        alias = tenant.shard.alias if tenant is not None else "default"
        token = current_db.set(alias)                     # axis 1: router -> shard DB
        switched = alias != "default"
        if switched:
            connections[alias].set_tenant(tenant)         # axis 2: search_path on the shard conn
        try:
            return self.get_response(request)
        finally:
            if switched:
                connections[alias].set_schema_to_public()  # reset - prevents cross-tenant leak
            current_db.reset(token)