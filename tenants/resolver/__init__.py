"""Tenant-resolution subsystem.

Layers (import from `tenants.resolver`, not the submodules):
  service.py   — the `resolve()` facade the middleware calls (orchestration)
  cache.py     — snapshot cache primitives (positive/negative/tombstone, warm, sweep)
  registry.py  — the `treg:hosts` SET gate (membership, reconcile, fenced lock)
  throttle.py  — fill_cap rate-limiter + single-flight coalescing (NOT the gate decision)
  markers.py   — cache sentinels (NEGATIVE, TOMBSTONE)

Design + event sequences: deploy/resolve_gate_design.md.
"""
from .cache import CacheUnavailable, TenantResolveCache, resolve_cache
from .markers import NEGATIVE, TOMBSTONE
from .registry import (
    DIRTY_KEY,
    HOSTS_KEY,
    HOSTS_NEW_KEY,
    WARM_LOCK_KEY,
    WARM_PENDING_KEY,
    HostRegistry,
    host_registry,
)
from .service import resolve
from .throttle import FillCap, fill_cap, single_flight

__all__ = [
    "resolve",
    "resolve_cache", "TenantResolveCache", "CacheUnavailable",
    "host_registry", "HostRegistry",
    "fill_cap", "FillCap", "single_flight",
    "NEGATIVE", "TOMBSTONE",
    "HOSTS_KEY", "HOSTS_NEW_KEY", "DIRTY_KEY", "WARM_LOCK_KEY", "WARM_PENDING_KEY",
]
