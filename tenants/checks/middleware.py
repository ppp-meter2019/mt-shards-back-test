"""MT middleware presence & order invariant. See settings_multitenant.py (_MT_INSERTS)."""
from django.conf import settings
from django.core.checks import Error, register

from .base import mt_check


@register()
@mt_check
def mt_middleware_order(app_configs, **kwargs):
    """tenants.E004 — under multi-tenant, the tenant middlewares must be PRESENT and in the
    right RELATIVE order (not necessarily adjacent). settings_multitenant.py builds MIDDLEWARE
    as a delta over the standalone base, so this guards against (a) an anchor/insert getting
    lost and (b) someone reordering the list into an unsafe sequence. Two ordered chains:
      CorsMiddleware -> ShardAwareTenantMiddleware -> TenantShardRoutingMiddleware
      AuthenticationMiddleware -> SchemaBoundSessionMiddleware  (schema-bound session needs
                                                                 request.user)
    DiagnosticsHeadersMiddleware is intentionally NOT required here."""
    mw = list(getattr(settings, "MIDDLEWARE", None) or [])
    chains = [
        ("corsheaders.middleware.CorsMiddleware",
         "tenants.middleware.ShardAwareTenantMiddleware",
         "tenants.middleware.TenantShardRoutingMiddleware"),
        ("django.contrib.auth.middleware.AuthenticationMiddleware",
         "users.middleware.SchemaBoundSessionMiddleware"),
    ]
    pos, errors = {}, []
    for chain in chains:                                     # presence
        for name in chain:
            if name not in mw:
                errors.append(Error(
                    f"MIDDLEWARE (multi-tenant) is missing required middleware {name!r}.",
                    hint="settings_multitenant.py inserts the tenant middlewares over the "
                         "base list; a missing one means an anchor/insert was lost.",
                    id="tenants.E004",
                ))
            else:
                pos[name] = mw.index(name)
    for chain in chains:                                     # relative order
        present = [n for n in chain if n in pos]
        for a, b in zip(present, present[1:]):
            if pos[a] > pos[b]:
                errors.append(Error(
                    f"MIDDLEWARE (multi-tenant): {a!r} must come BEFORE {b!r}.",
                    hint="CORS stays outermost, tenant resolution precedes shard routing, and "
                         "SchemaBoundSessionMiddleware must follow AuthenticationMiddleware "
                         "(it reads request.user).",
                    id="tenants.E004",
                ))
    return errors
