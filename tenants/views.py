"""Runtime views for the `tenants` app.

Exactly one lives here, and deliberately so: `health` is imported by BOTH URLconfs
(urls_public.py AND urls_tenant.py), which makes it request-path infrastructure rather than
operator tooling. Everything else that used to be in this module — the Shard / Tenant /
ReservedHostRule viewsets and the base-domains list — moved to `tenants/console/views.py`;
see tenants/console/__init__.py for the boundary and why it is a subpackage.
"""
from django.http import HttpResponse


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
