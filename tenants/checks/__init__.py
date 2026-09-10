"""Deploy-time system checks (invariants) for multi-tenant mode.

Registered from tenants.apps.ready() by importing this package (each submodule's @register()
fires on import). They run on `manage.py check` and automatically before migrate/runserver —
the CI/deploy gate. NB: gunicorn does NOT run system checks at WSGI boot, so these are
deploy-time guards, not per-boot ones; the runtime keeps its own fail-safe defaults.

Grouped by concern:
  * gate.py       — E001: resolve-gate flag invariants (GATE requires WARM)
  * beat.py       — E002/E003/E005/E006: Celery beat schedule + fanout-dispatch contract
  * middleware.py — E004: MT middleware presence & relative order

Convention: every cross-layer / cross-module contract that would otherwise fail silently — or
only at runtime — earns an E00x check here. All are MT-only (via the base.mt_check decorator);
standalone has none of these surfaces.
"""
from .base import mt_check
from .gate import gate_requires_warm
from .beat import (
    beat_grace_ge_fanout_period,
    beat_entries_wrapped,
    fanout_entries_are_unique,
    fanout_task_registered,
)
from .middleware import mt_middleware_order

__all__ = [
    "mt_check",
    "gate_requires_warm",
    "beat_grace_ge_fanout_period",
    "beat_entries_wrapped",
    "fanout_task_registered",
    "fanout_entries_are_unique",
    "mt_middleware_order",
]
