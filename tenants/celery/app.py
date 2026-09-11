"""CeleryApp + per-invocation schema switch — multi-DB adaptation of
tenant_schemas_celery.app.

Each task enters its tenant's SHARD + schema inside TenantTask.__call__ (a with-block that
sets current_db -> the shard AND the schema on that shard's connection); the context
manager's finally restores BOTH when the task returns or raises. Doing it per invocation —
not via task_prerun/postrun signals, which have no shared finally and stashed the context
manager on the task singleton — makes restore crash-safe and pool-safe.

Guardrail: refuse cooperative worker pools (gevent/eventlet). The current_db ContextVar
routing assumes NON-cooperative concurrency (prefork/solo/threads — each task in its own
process or thread-context); under a cooperative pool, greenlets share one context and can
cross tenant schemas.
"""
from collections.abc import Sequence
from typing import Any

from celery import Celery
from celery.result import AsyncResult
from celery.signals import celeryd_init
from django.core.exceptions import ImproperlyConfigured

from .task import headers_with_schema

_UNSAFE_POOLS = {"gevent", "eventlet"}


@celeryd_init.connect
def _guard_worker_pool(sender: Any = None, instance: Any = None, conf: Any = None,
                       options: dict[str, Any] | None = None, **_: Any) -> None:
    """Fail LOUD at worker start if the pool is cooperative — silent tenant crossover is a
    far worse outcome than a refused boot."""
    pool = (options or {}).get("pool") or getattr(conf, "worker_pool", None) or "prefork"
    if pool in _UNSAFE_POOLS:
        raise ImproperlyConfigured(
            f"Celery worker pool {pool!r} is unsafe for this project: tenant routing uses a "
            f"`current_db` ContextVar + a per-task schema switch that require non-cooperative "
            f"concurrency (prefork/solo/threads — each task in its own process or thread "
            f"context). Under {pool}, greenlets share one context and can cross tenant "
            f"schemas. Run with --pool=prefork (the project default)."
        )


class CeleryApp(Celery):
    registry_cls = "tenants.celery.registry:TenantTaskRegistry"
    task_cls = "tenants.celery.task:TenantTask"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("task_cls", self.task_cls)
        super().__init__(*args, **kwargs)

    def create_task_cls(self) -> type:
        return self.subclass_with_self(
            self.task_cls, abstract=True, name="TenantTask", attribute="_app",
        )

    def send_task(self, name: str, args: Sequence[Any] | None = None,
                  kwargs: dict[str, Any] | None = None, **options: Any) -> AsyncResult:
        options["headers"] = headers_with_schema(options.get("headers") or {})
        return super().send_task(name, args=args, kwargs=kwargs, **options)
