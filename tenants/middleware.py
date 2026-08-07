"""Tenant + shard routing middleware (sync).

Two cooperating SYNC middlewares wire BOTH axes of multi-DB multi-tenancy (the schema
must be set on the same connection/thread the ORM later uses):

  A. ShardAwareTenantMiddleware - resolves the tenant from the Host (+ its shard in one
     query), sets request.tenant + the schema on the DEFAULT connection, gates on
     status, and delegates host->Tenant caching to tenants.resolve_cache. The cache is
     an optimization: any cache-layer failure degrades to a plain DB resolve.

  B. TenantShardRoutingMiddleware - reads request.tenant, points the router at the
     shard (current_db) and sets/reset the tenant schema on the SHARD connection.
     Must be listed AFTER ShardAwareTenantMiddleware.
"""
import logging

from django.db import OperationalError, connections
from django.http import Http404, HttpResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name

from .context import current_db
from .errors import error_response
from .models import Tenant
from .resolve_cache import resolve_cache

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
        except OperationalError:
            # DB unreachable during resolution — branded 500 instead of a raw 500.
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
        if not resolve_cache.enabled:
            return self._resolve_tenant(domain_model, hostname)
        # The cache is an optimization: ANY cache-layer failure (a non-redis backend
        # rejecting nx=True, a corrupt/unpicklable entry, ...) must degrade to a plain
        # DB resolve, never 500 this per-request path. DoesNotExist is a real answer
        # ("no tenant for this host"), so it is propagated, not treated as a failure.
        try:
            return self._get_tenant_cached(domain_model, hostname)
        except domain_model.DoesNotExist:
            raise
        except OperationalError:
            raise   # DB down — surface for the 500 handler; don't retry a dead DB
        except Exception:
            logger.warning("tenant_resolve cache path failed for %r; falling back to DB",
                           hostname, exc_info=True)
            return self._resolve_tenant(domain_model, hostname)

    def _get_tenant_cached(self, domain_model, hostname):
        snap = resolve_cache.get_snapshot(hostname)
        if snap is resolve_cache.NEG:
            raise domain_model.DoesNotExist(hostname)   # cached miss — no DB hit
        if snap is not resolve_cache.MISS:
            return snap                                  # reconstructed Tenant (read_only)
        try:
            tenant = self._resolve_tenant(domain_model, hostname)
        except domain_model.DoesNotExist:
            resolve_cache.store_miss(hostname)
            raise
        resolve_cache.store(hostname, tenant)
        return tenant

    @staticmethod
    def _resolve_tenant(domain_model, hostname):
        tenant = (
            domain_model.objects
            .select_related("tenant__shard")
            .get(domain=hostname)
            .tenant
        )
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