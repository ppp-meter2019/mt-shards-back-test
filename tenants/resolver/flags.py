"""Single source of truth for the resolver rollout flags. Reads the TENANT_REGISTRY
namespace via registry_cfg (config.py); both the cache (write side) and the registry
(gate side) go through these.

NB: the tenants.E001 system check (tenants/checks.py) deliberately reads the RAW
TENANT_REGISTRY dict, NOT gate_enabled() — it must see a GATE-on/WARM-off misconfig that
the fail-safe below hides.
"""
from .config import registry_cfg


def warm_enabled():
    return bool(registry_cfg.WARM_ENABLED)


def gate_enabled():
    # Fail-safe: the gate is effective ONLY when WARM is also on — WARM is the write side
    # that builds the tres:hosts SET the gate reads. GATE-without-WARM is treated as OFF in
    # every process; the misconfig is surfaced by the tenants.E001 system check.
    return bool(registry_cfg.GATE_ENABLED) and warm_enabled()
