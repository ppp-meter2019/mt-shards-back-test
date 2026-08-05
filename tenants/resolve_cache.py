"""Single source of truth for invalidating CACHES["tenant_resolve"].

Used by the post_save/post_delete signals (admin-driven .save()/.delete()) AND —
crucially — by the status machine, which mutates via QuerySet.update() and so does
NOT fire those signals. Every status/shard change made with .update() must call
forget_tenant() explicitly; the signals alone are not enough.

Invalidation writes a short-lived TOMBSTONE (not a plain delete): while it is
present, get_tenant treats the key as a miss (resolves DB-direct) and its nx=True
populate cannot overwrite it, so a slow resolver that read a pre-change row cannot
repopulate a stale snapshot. This closes the read-then-write race at the cache
layer (no Celery). Self-heals when the marker expires.
"""

from django.conf import settings
from django.core.cache import caches
from django.db import transaction

from .resolve_markers import TOMBSTONE


def forget_hosts(hostnames):
    hostnames = [h for h in hostnames if h]
    if not hostnames:
        return
    cache = caches["tenant_resolve"]
    hold = getattr(settings, "TENANT_RESOLVE_HOLD_SECONDS", 5)

    def _invalidate():
        if hold:
            # Overwrite whatever is cached with a short hold marker.
            cache.set_many({h: TOMBSTONE for h in hostnames}, hold)
        else:
            cache.delete_many(hostnames)   # hold disabled => plain delete (race open)

    transaction.on_commit(_invalidate)


def forget_tenant(tenant):
    """Drop every domain of `tenant`. Call after any status/shard change made via
    QuerySet.update() (which bypasses the post_save signal)."""
    forget_hosts(tenant.domains.values_list("domain", flat=True))