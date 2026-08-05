"""Tenant + shard routing middleware (sync).

Two cooperating middlewares wire BOTH axes of multi-DB multi-tenancy. They are
SYNC on purpose: the schema must be set on the SAME connection (same thread)
that the ORM query later uses, so the schema-setting cannot live on an async
event-loop middleware. See README "Architecture trade-offs".

  A. ShardAwareTenantMiddleware - subclasses django-tenants' TenantMainMiddleware.
     Resolves the tenant from the Host (pulling its shard in the same query) and
     sets request.tenant + the schema on the DEFAULT connection. Shared models
     (Tenant/Domain/public users) resolve on default.public via the search_path
     public-fallback.

  B. TenantShardRoutingMiddleware - reads request.tenant and wires the SHARD:
     - axis 1 (which DB):     sets the current_db ContextVar the router reads;
     - axis 2 (which schema): sets the tenant schema on the SHARD connection,
       and RESETS it on the way out. The reset is critical for isolation: shard
       connections are persistent (CONN_MAX_AGE) and shared by every tenant on
       that shard - without the reset, the next request for a different tenant
       on the same shard would inherit this search_path and read its data.

     Must be listed AFTER ShardAwareTenantMiddleware (it needs request.tenant).
"""

import logging

from django.conf import settings
from django.core.cache import caches
from django.db import connections
from django.http import HttpResponse, JsonResponse
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.utils import get_public_schema_name

from .context import current_db
from .models import Shard, Tenant
from .resolve_markers import NEGATIVE, TOMBSTONE

logger = logging.getLogger(__name__)


def _dump(tenant):
    """Serialize the routing snapshot (all scalar fields of the tenant + its
    shard) into a plain dict for the resolution cache. Capturing fields
    dynamically means a newly added field is included automatically."""
    shard = tenant.shard
    return {
        "tenant": {f.attname: getattr(tenant, f.attname) for f in tenant._meta.fields},
        "shard":  {f.attname: getattr(shard,  f.attname) for f in shard._meta.fields},
    }


def _build(model, values):
    """Rebuild a model instance, tolerant of schema drift across deploys: keep
    only attrs that are still fields (a removed/renamed field in an old cached
    entry is ignored; a new field just takes its model default). Avoids a hard
    cache flush on such migrations."""
    fields = {f.attname for f in model._meta.fields}
    return model(**{k: v for k, v in values.items() if k in fields})


def _load(data):
    """Reconstruct a Tenant(+shard) from a cached snapshot. Both are flagged
    read_only so save()/delete() refuse to touch the DB — the instance carries only
    cached fields and would clobber/remove the real rows."""
    tenant = _build(Tenant, data["tenant"])
    shard = _build(Shard, data["shard"])
    shard.read_only = True
    tenant.shard = shard                 # real pk -> tenant.shard_id consistent; .alias needs no DB
    tenant.read_only = True
    return tenant


class ShardAwareTenantMiddleware(TenantMainMiddleware):
    """TenantMainMiddleware that pulls the tenant's shard in the same query, so
    TenantShardRoutingMiddleware can read request.tenant.shard without an extra
    round-trip."""

    # Liveness paths answered HERE, by the OUTERMOST middleware, before anything
    # downstream runs. The ALB health-checks the target by IP, so the Host is the
    # instance IP - which fails BOTH tenant resolution (no Domain) AND ALLOWED_HOSTS
    # (DisallowedHost, raised by SecurityMiddleware/CommonMiddleware's get_host()).
    # Short-circuiting with a static 200 skips both: the response reflects nothing
    # of the Host, so bypassing ALLOWED_HOSTS for this one path is safe.
    HEALTH_PATHS = frozenset({"/api/health/"})

    def process_request(self, request):
        if request.path in self.HEALTH_PATHS:
            return HttpResponse("ok", content_type="text/plain")

        # Resolve the tenant (sets request.tenant + the schema on `default`).
        response = super().process_request(request)
        if response is not None:
            # No tenant / DisallowedHost / public fallback already produced a
            # response — pass it through unchanged.
            return response

        # Gate on tenant status — only ACTIVE business tenants may be served (their
        # whole host, API + admin). The PUBLIC tenant is EXEMPT: management lives on
        # the public host, which must stay reachable regardless of any status glitch.
        #   - DEACTIVATED -> 403 (intentionally closed — whole host incl. admin)
        #   - NEW/PENDING -> 503 (schema not provisioned/ready; the Domain exists from
        #     tenant creation, so without this the request would 500 at the DB layer
        #     against a missing/incomplete schema)
        #   - FAILED      -> 503 for the API, but the tenant's OWN /admin/ is left
        #     reachable so an operator can log in and inspect (best-effort — a too-
        #     broken schema may still 500). Not a security downgrade: same auth as when
        #     ACTIVE; unlike DEACTIVATED, FAILED is an operational (not closed) state.
        tenant = getattr(request, "tenant", None)
        if tenant is not None and tenant.schema_name != get_public_schema_name():
            st = tenant.status
            if st == Tenant.Status.DEACTIVATED:
                return JsonResponse(
                    {"detail": "This tenant is deactivated.", "code": "tenant_deactivated"},
                    status=403,
                )
            if st != Tenant.Status.ACTIVE:
                # `/admin` (no slash) counts too — APPEND_SLASH would redirect it to
                # `/admin/`, but CommonMiddleware runs after us, so match it here.
                is_admin = request.path == "/admin" or request.path.startswith("/admin/")
                admin_ok = st == Tenant.Status.FAILED and is_admin
                if not admin_ok:
                    return JsonResponse(
                        {"detail": "This tenant is not ready.", "code": "tenant_not_ready",
                         "status": st},
                        status=503,
                    )
        return None

    def get_tenant(self, domain_model, hostname):
        pos_ttl = getattr(settings, "TENANT_RESOLVE_CACHE_SECONDS", 0)
        neg_ttl = getattr(settings, "TENANT_RESOLVE_MISS_CACHE_SECONDS", 0)
        if not (pos_ttl or neg_ttl):
            return self._resolve_tenant(domain_model, hostname)
        # The cache is an optimization: ANY cache-layer failure (a non-redis backend
        # rejecting nx=True, a corrupt/unpicklable entry, ...) must degrade to a plain
        # DB resolve, never 500 this per-request path. DoesNotExist is a real answer
        # ("no tenant for this host"), so it is propagated, not treated as a failure.
        try:
            return self._get_tenant_cached(domain_model, hostname, pos_ttl, neg_ttl)
        except domain_model.DoesNotExist:
            raise
        except Exception:
            logger.warning(
                "tenant_resolve cache path failed for %r; falling back to DB",
                hostname, exc_info=True,
            )
            return self._resolve_tenant(domain_model, hostname)

    def _get_tenant_cached(self, domain_model, hostname, pos_ttl, neg_ttl):
        cache = caches["tenant_resolve"]
        cached = cache.get(hostname)                       # None on absent OR Redis error (fail-open)
        if cached is not None and cached != TOMBSTONE:     # tombstone => treat as miss (hold window)
            if cached == NEGATIVE:
                raise domain_model.DoesNotExist(hostname)  # cached miss — no DB hit
            return _load(cached)                           # cached routing snapshot

        try:
            tenant = self._resolve_tenant(domain_model, hostname)
        except domain_model.DoesNotExist:
            if neg_ttl:
                cache.set(hostname, NEGATIVE, neg_ttl, nx=True)    # nx: never clobber a hold marker
            raise
        if pos_ttl:
            cache.set(hostname, _dump(tenant), pos_ttl, nx=True)   # nx: never clobber a hold marker
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
        # fresh (here) or rebuilt from cache (_load). Refuse save()/delete() either way
        # so behavior never depends on cache state (miss vs hit).
        tenant.read_only = True
        tenant.shard.read_only = True
        return tenant


class TenantShardRoutingMiddleware:
    """Routes the ORM to the tenant's shard and sets that shard connection's
    schema, resetting both on the way out."""

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
