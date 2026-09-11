"""Resolve-gate flag invariants. Full design: deploy/resolve_gate_design.md."""
from typing import Any

from django.apps import AppConfig
from django.core.checks import CheckMessage, Error, register

from .base import mt_check


@register()
@mt_check
def gate_requires_warm(app_configs: list[AppConfig] | None,
                       **kwargs: Any) -> list[CheckMessage]:
    """tenants.E001 — TENANT_REGISTRY['GATE_ENABLED'] requires ['WARM_ENABLED']. Reads the
    RAW dict (NOT registry_cfg.gate_enabled) so the fail-safe can't hide the misconfig.

    NB: gunicorn does NOT run system checks at WSGI boot, so this is a deploy-time guard, not
    a per-boot one. The per-boot safety is in code: host_registry.gate_enabled treats
    GATE-without-WARM as OFF (fail-safe). This check makes that misconfig LOUD at deploy time
    instead of silently ignoring the operator's GATE flag."""
    from django.conf import settings
    reg = getattr(settings, "TENANT_REGISTRY", None) or {}
    gate = reg.get("GATE_ENABLED", False)
    warm = reg.get("WARM_ENABLED", False)
    if gate and not warm:
        return [Error(
            "TENANT_REGISTRY['GATE_ENABLED'] is on but ['WARM_ENABLED'] is off.",
            hint=(
                "The gate reads the treg:hosts SET that only the WARM write side "
                "builds/maintains (signals + reconcile). With WARM off the SET is "
                "never created, so the gate can never hard-reject. As a safety net "
                "host_registry.gate_enabled treats this combination as OFF — meaning "
                "your GATE flag is being SILENTLY IGNORED. Set WARM_ENABLED in "
                "TENANT_REGISTRY first, run `manage.py warm_resolve_cache` to build the "
                "SET, verify with `resolve_cache_bench.py inspect`/`gate`, then enable the gate."
            ),
            id="tenants.E001",
        )]
    return []
