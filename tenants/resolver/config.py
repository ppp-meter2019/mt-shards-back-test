"""Namespaced resolver settings: two dicts (TENANT_RESOLVE / TENANT_REGISTRY) over in-code
DEFAULTS, per-key merge, read LIVE. No cache / no import-strings — a read is a cheap
dict.get, so override_settings (tests) and direct settings mutation (bench) just work,
with no setting_changed dance.

Access is attribute-style: `resolve_cfg.HOLD_SECONDS`, `registry_cfg.WARM_ENABLED`. A key
present in the user's dict wins; otherwise the DEFAULTS value is returned. The flags facade
(flags.py) reads WARM_ENABLED/GATE_ENABLED through registry_cfg; the E001 system check reads
the raw dict itself (it must see a GATE-on/WARM-off misconfig that fail-safe hides).
"""
from django.conf import settings

RESOLVE_DEFAULTS = {
    "POSITIVE_CACHE_SECONDS": 3600,     # positive (success) snapshot TTL — gate-off mode + WARM fallback
    "MISS_CACHE_SECONDS": 60,           # negative (miss) marker TTL
    "HOLD_SECONDS": 5,                  # invalidation hold (tombstone) TTL
    "WARM_TTL_BY_STATUS": {             # positive TTL by tenant status, used ONLY under WARM
        "active": None, "deactivated": 3600, "failed": 1800, "new": 120, "pending": 120,
    },
    "FILLCAP_PER_SEC": 20,              # global cap on flag-absent DB resolves
    "FILLCAP_LOCAL_PER_SEC": 5,         # per-pod fallback when the global counter is down
}
REGISTRY_DEFAULTS = {
    "WARM_ENABLED": False,              # write side: maintain tres:hosts + ttl_by_status
    "GATE_ENABLED": False,              # read side: hard-reject non-members (requires WARM)
    "HOSTS_ARM_SECONDS": 300,           # dead-man TTL armed on domain mutations
    "RECONCILE_SECONDS": 86400,         # daily safety reconcile — consumed by the ops beat schedule, NOT by code
    "WARM_LOCK_SECONDS": 120,           # tres:warming single-writer lock TTL
    "WARM_PENDING_SECONDS": 10,         # reconcile enqueue-coalescing window
}


class _Namespace:
    """Attribute view over one settings dict, merged per-key over DEFAULTS (live)."""

    def __init__(self, setting_name, defaults):
        self._setting_name = setting_name
        self._defaults = defaults

    def __getattr__(self, name):
        # __getattr__ only fires for names NOT found normally. Underscore names (dunder
        # probes from copy/pickle, or an access before __init__ populated _defaults) must
        # raise immediately — WITHOUT touching self._defaults, or that touch would itself
        # miss and recurse into __getattr__ forever.
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._defaults:
            raise AttributeError(f"{self._setting_name} has no key {name!r}")
        user = getattr(settings, self._setting_name, None) or {}
        return user.get(name, self._defaults[name])


resolve_cfg = _Namespace("TENANT_RESOLVE", RESOLVE_DEFAULTS)
registry_cfg = _Namespace("TENANT_REGISTRY", REGISTRY_DEFAULTS)
