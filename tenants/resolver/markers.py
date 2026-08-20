"""Sentinel values stored in CACHES["tenant_resolve"], shared by the read path
(resolver.service via resolver.cache.get_snapshot) and the invalidation path
(resolver.cache.forget_*).

Value-compared, not identity — they survive the Redis serialization round-trip,
and a resolved-tenant snapshot (a dict) never equals either.
"""

# A host resolved as "no such domain" (negative cache entry).
NEGATIVE = "\x00no-tenant\x00"

# Short-lived "hold" marker written on invalidation (see resolver.cache): while it is
# present, get_snapshot treats the key as a miss and an nx populate (put/store) cannot
# overwrite it — closing the read-then-write race.
TOMBSTONE = "\x00hold\x00"
