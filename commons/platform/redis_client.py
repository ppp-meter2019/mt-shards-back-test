"""The TENANT-SCOPED choke point for direct redis-py access.

There are exactly TWO sanctioned ways to get a raw redis-py client in this project.
scripts/ci_guard_redis_client.sh is a STATIC CHECK that reports any third one — nothing runs
it automatically today, so it only fires when someone invokes it from scripts/:

  * `tenants.resolver.cache.ResolveCache.get_redis_raw_client()` — over CACHES['tenant_resolve'],
    deliberately tenant-AGNOSTIC (host -> tenant resolution runs BEFORE the schema is known).
  * `tenant_raw_client()` here — over CACHES['default'], for keys that belong to ONE tenant.

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


def tenant_raw_client(write: bool = True) -> Any:
    """The underlying redis-py client for CACHES['default'], bypassing the cache wrapper.

    For operations the Django cache API does not expose — HSET/HGETALL, SET NX/EX, INCR,
    EXPIRE, SCAN, pipelines. Build keys with cache_keys.tenant_key().

    :param write: False routes to a replica when one is configured; read-only ops only.
    """
    return caches["default"].client.get_client(write=write)
