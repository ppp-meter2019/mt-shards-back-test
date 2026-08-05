"""Sentinel values stored in CACHES["tenant_resolve"], shared by the read path
(tenants.middleware) and the invalidation path (tenants.resolve_cache).

Value-compared, not identity — they survive the Redis serialization round-trip,
and a resolved-tenant snapshot (a dict) never equals either.
"""

# A host resolved as "no such domain" (negative cache entry).
NEGATIVE = "\x00no-tenant\x00"

# Short-lived "hold" marker written on invalidation (see resolve_cache): while it
# is present, get_tenant treats the key as a miss and an nx=True populate cannot
# overwrite it — closing the read-then-write race.
TOMBSTONE = "\x00hold\x00"
