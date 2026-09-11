"""Tenant + shard routing middleware (sync).

Two cooperating SYNC middlewares wire BOTH axes of multi-DB multi-tenancy (the schema
must be set on the same connection/thread the ORM later uses):

  A. ShardAwareTenantMiddleware - resolves the tenant from the Host (+ its shard in one
     query), sets request.tenant + the schema on the DEFAULT connection, gates on
     status, and delegates host->Tenant caching to tenants.resolver. The cache is
     an optimization: any cache-layer failure degrades to a plain DB resolve. The one
     exception is ResolveDeferred (the gate shedding load when the host registry is
     unavailable and the DB budget is spent) -> retryable 503, never 404.

  B. TenantShardRoutingMiddleware - reads request.tenant, points the router at the
     shard (current_db) and sets/reset the tenant schema on the SHARD connection.
     Must be listed AFTER ShardAwareTenantMiddleware.
"""
import logging
from collections.abc import Callable

import psycopg

from django.db import InterfaceError, OperationalError, connections
from django.http import Http404, HttpRequest, HttpResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name

from .context import tenant_context, use_alias
from .errors import error_response
from .models import Domain, Tenant
from .resolver import ResolveDeferred, TenantSnapshot, resolve as resolve_tenant

logger = logging.getLogger(__name__)


class ShardAwareTenantMiddleware(TenantMainMiddleware):
    """TenantMainMiddleware that pulls the tenant's shard in the same query and gates
    on tenant status."""

    # Liveness answered HERE (outermost) before anything downstream: the ALB health-
    # checks by IP, so the Host is the instance IP — fails tenant resolution AND
    # ALLOWED_HOSTS. A static 200 skips both.
    HEALTH_PATHS = frozenset({"/api/health/"})

    def process_request(self, request: HttpRequest) -> HttpResponse | None:
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
        except ResolveDeferred:
            # The gate DECLINED to resolve: the host registry was unavailable AND the DB
            # budget for that ambiguity was spent (resolver.service). We do NOT know the
            # host is unknown, so a 404 would be a claim we never established — and it
            # would tell a legitimate tenant's users their workspace is gone. 503 + a short
            # Retry-After is the honest answer and lets clients back off. Nothing was
            # negative-cached (the reject happens before _fill), so the next attempt is a
            # clean one — hence seconds, not minutes. Logged (rate-limited) in the resolver.
            return error_response(
                request, status=503, code="tenant_resolve_deferred",
                detail="Temporarily unable to resolve this workspace. Please retry.",
                template="tenants/errors/database_error.html",
                retry_after=5,
            )
        except (OperationalError, InterfaceError,
                psycopg.OperationalError, psycopg.InterfaceError):
            # DB unreachable during resolution — branded 500 instead of a raw 500.
            # psycopg.* is caught too: django-tenants runs `SET search_path` on a RAW psycopg
            # cursor, so a pool/proxy borrow-timeout can escape UNWRAPPED (not as a django.db.*
            # error) from the schema-set step outside get_tenant. InterfaceError covers a
            # closed/broken connection (a stale pool/proxy borrow) — psycopg raises it as a
            # SIBLING of OperationalError (not a subclass), so it must be listed explicitly.
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

    def get_tenant(self, domain_model: type[Domain], hostname: str) -> TenantSnapshot:
        # All resolve policy (cache, gate, fill_cap, coalescing, fail-open) lives in the
        # resolver service facade. The middleware only supplies the DB-resolver closure
        # (_resolve_tenant — the authoritative lookup) and the "not found" exception.
        return resolve_tenant(
            hostname,
            lambda: self._resolve_tenant(domain_model, hostname),
            domain_model.DoesNotExist,
        )

    @staticmethod
    def _resolve_tenant(domain_model: type[Domain], hostname: str) -> TenantSnapshot:
        try:
            tenant = (
                domain_model.objects
                .select_related("tenant__shard")
                .get(domain=hostname)
                .tenant
            )
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            # django-tenants sets search_path on a RAW psycopg cursor, so a DB/pool error
            # (a pool / RDS-Proxy borrow-timeout: psycopg ConnectionException; or a
            # closed/broken connection: psycopg.InterfaceError) escapes UNWRAPPED here — NOT as
            # a django.db.* error. Normalize it so get_tenant surfaces it as a DB outage
            # (branded 5xx via process_request) instead of mislabeling it a cache failure and
            # retrying a dead DB.
            raise OperationalError(str(exc)) from exc
        # Narrow to the routing snapshot HERE, not only on the cache path: a MISS must
        # expose exactly what a HIT exposes. Handing out the full row here is what made
        # request.tenant.company_name work on a cold cache and silently read "" on a warm
        # one — the divergence the snapshot type exists to remove.
        return TenantSnapshot.capture(tenant)


class TenantShardRoutingMiddleware:
    """Routes the ORM to the tenant's shard and sets that shard connection's schema,
    resetting both on the way out.

    Both axes are wired by tenants.context — the ONE implementation of the enter/exit dance,
    shared with TenantTask and tenant_command. Delegating matters here: entering the two axes
    by hand invites setting the schema before the try, which leaks current_db for the life of
    the thread when the alias is missing from settings.DATABASES. The one thing NOT delegated
    is the exit reset — see the finally in __call__.
    """

    sync_capable = True
    async_capable = False

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        tenant = getattr(request, "tenant", None)
        if tenant is None or tenant.shard.alias == "default":
            # Public tenant, or no tenant at all: the schema on `default` was already set by
            # ShardAwareTenantMiddleware, which also re-resets it to public at the START of
            # every request (django_tenants middleware/main.py:35) — so there is nothing to
            # set and nothing to reset here. Pin only the router axis, and pin it EXPLICITLY
            # rather than inheriting whatever this thread last left in current_db.
            with use_alias("default"):
                return self.get_response(request)

        alias = tenant.shard.alias
        # Resolve the connection FIRST: an alias missing from settings.DATABASES (a Shard row
        # whose alias was dropped) raises HERE, before either axis is touched — no half-entered
        # state to leak — and it cannot be masked by the finally below.
        conn = connections[alias]
        try:
            with tenant_context(tenant):        # axis 1 + axis 2, restore-safe, one impl
                return self.get_response(request)
        finally:
            # Deliberately NOT delegated to tenant_context. _switch restores the connection's
            # PREVIOUS tenant — the right contract for a nestable helper, and on this path it
            # happens to be public anyway. Here we want the STRONGER, unconditional guarantee:
            # whatever was there, this shard connection ends on `public`. On a shard that
            # schema holds ONLY the postgis extension (routers.allow_migrate keeps every app
            # table out of it), so a stray query on this connection between requests fails
            # loudly with "relation does not exist" instead of silently reading another
            # tenant's rows.
            conn.set_schema_to_public()
