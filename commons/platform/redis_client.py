"""The TENANT-SCOPED choke point for direct redis-py access.

There are exactly TWO sanctioned ways to get a raw redis-py client in this project.
scripts/ci_guard_redis_client.sh is a STATIC CHECK that reports any third one — nothing runs
it automatically today, so it only fires when someone invokes it from scripts/:

  * `tenants.resolver.cache.ResolveCache.get_redis_raw_client()` — over CACHES['tenant_resolve'],
    deliberately tenant-AGNOSTIC (host -> tenant resolution runs BEFORE the schema is known).
  * `django_redis_raw_client(alias)` here — over any django_redis alias. The ALIAS at the call site is
    the contract, and each one differs:
        default         tenant-scoped; every key MUST come from cache_keys.tenant_key()
        beat_lock       the broker Redis (noeviction); coordination keys only, never data
        tenant_resolve  reachable, but go through the resolver's own accessor instead

Why a choke point at all: `KEY_FUNCTION` rewrites only the keys that pass through the Django
cache API. It does nothing for a raw client, and the structures that most need tenant
scoping are exactly the raw ones — the four aggregate Redis hashes of RGKB section 28
(coordinates_package, user_coordinates_package, opened_orders, orders:violations_update) and
the VTL keys. So the discipline has to be enforced somewhere else, and "somewhere else" is a
short allowlist plus a static guard.

THE CLIENT IS NOT SCOPED - THE KEY IS. Holding this client gives you the whole keyspace of
that Redis. Build EVERY key through `commons.platform.cache_keys.tenant_key()`, which raises
off-tenant rather than silently addressing another namespace. Reviewing a call site here
means checking the keys, not the client.

ERRORS PROPAGATE — and under multi-tenant so does the wrapped cache. CACHES['default'] sets
IGNORE_EXCEPTIONS=False there (settings_multitenant.py), so a Redis outage raises on BOTH
paths; the standalone base keeps the alias fail-open, which is the only place the two differ.
Either way, callers here must decide explicitly whether their path fails open or closed —
nothing above them will. The resolver's raw client behaves the same, but its alias
(tenant_resolve) is fail-open, so there the raw/wrapped distinction is real.
"""
from typing import Any

from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured


def django_redis_raw_client(alias: str, write: bool = True) -> Any:
    """The underlying redis-py client for CACHES[alias], bypassing the Django cache wrapper.

    For operations the cache API does not express — HSET/HGETALL, SET NX/EX, INCR, EXPIRE,
    SCAN, pipelines, fenced locks. See the alias contracts in this module's docstring; for
    `default` that means building every key through cache_keys.tenant_key().

    Raises ImproperlyConfigured when the alias is not backed by django_redis. That is a real
    case, not a defensive flourish: tests and local setups swap an alias for LocMemCache, which
    has no `.client` at all, and the bare AttributeError it would otherwise raise names neither
    the alias nor the reason.

    :param alias: a key of settings.CACHES. An unknown one raises InvalidCacheBackendError.
    :param write: False routes to a replica when one is configured; read-only ops only.
    """
    cache = caches[alias]
    if not hasattr(getattr(cache, "client", None), "get_client"):
        raise ImproperlyConfigured(
            f"CACHES[{alias!r}] is {type(cache).__name__}, which exposes no redis-py client. "
            f"django_redis_raw_client() needs a django_redis backend; use the Django cache API, or "
            f"point this alias at django_redis.cache.RedisCache."
        )
    # Written as one `cache.client.get_client(...)` expression on purpose: ci_guard_ast.py
    # matches `.get_client` only when its receiver is an attribute named `client`, so binding
    # the client to a local first would make this very file — the sanctioned choke point —
    # invisible to the guard that exists to find raw access. That is not hypothetical: an
    # earlier draft of this function did exactly that and dropped the guard's match count from
    # two files to one.
    return cache.client.get_client(write=write)
