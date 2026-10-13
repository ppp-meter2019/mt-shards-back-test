"""Tenant provisioning / housekeeping tasks.

Queue placement — MT-only; task_queue() returns None in standalone, so every task below
falls back to the host's default queue there:
  provision_tenant, drop_tenant_schema_task  -> `service`  management operations, not
      business tasks, and long-running (provisioning runs migrate_schemas).
  reconcile_host_registry_task               -> `fast`     NOT `service`: time-to-warm is
      on the availability path, and the cost is bounded by the treg:warming lock rather
      than by the queue. Full rationale at its decorator.

provision_tenant calls the migrator core DIRECTLY (tenants.migrator.migrate_tenant, no
call_command; deploy/migrations_plan_rev7.md §15.1): under the tenant's lease it creates
the schema on its shard, migrates it and moves NEW/PENDING -> ACTIVE (or FAILED). No log
files (those are a CLI feature). Only NEW/PENDING are taken; anything else is skipped.

  skipped `locked` / `code_older_than_db` -> retry in 5 min, up to 15 times: another run
      holds the lease (or its backend is still working), or this worker is the OLD code
      during a rollout — a new worker picks it up. While THIS task keeps retrying, it also
      resumes a PENDING left by another run that died.
  failed -> ERROR in the log, no retry.

NO automatic recovery — the tenant stays PENDING (503) until an operator acts — when:
  * the run fails with a lost connection AFTER the claim (`failed conn_error`): the status
    is left PENDING on purpose (resumable), but nothing re-runs the task;
  * this task's worker process dies mid-run (SIGKILL, OOM): with acks_late and the default
    task_reject_on_worker_lost=False the message is acked, never redelivered;
  * the 15 retries run out (MaxRetriesExceededError).
The console cannot help: `provision` accepts only NEW, the admin does not let PENDING be
changed. `reconcile_tenants --check` reports it as `WARN stuck PENDING`; the fix is
`migrate_schemas -s <schema>` on a backend host (it resumes PENDING), after
`migration_lease --release <schema>` if a dead run left its lease (`--force` while it has
not expired, only once that process is certainly dead)
(deploy/migrations_zero_downtime.md §13). Connection hygiene (no migrator lock_timeout
left on this long-lived worker's connections) is the migrator's job: §7.

The generic fan-out scheduler infrastructure (fanout_dispatch / sub_dispatch / the tz
due-check) lives in tenants/celery/dispatch.py — imported at the bottom of THIS module so
Celery autodiscover (which imports `<app>.tasks`) registers those tasks too.
"""
from typing import Any

from celery import Task, shared_task
from celery.utils.log import get_task_logger
from django.core.management import call_command

from commons.platform.beat import task_queue

from . import migrator
from .models import Tenant

logger = get_task_logger(__name__)


PROVISION_RETRY_COUNTDOWN = 300
PROVISION_MAX_RETRIES = 15
_RETRY_REASONS = (migrator.Reason.LOCKED, migrator.Reason.CODE_OLDER_THAN_DB)


@shared_task(bind=True, queue=task_queue("service"), acks_late=True,
             max_retries=PROVISION_MAX_RETRIES)
def provision_tenant(self, tenant_id: int) -> dict[str, Any]:
    tenant = Tenant.objects.filter(pk=tenant_id).first()
    if tenant is None or tenant.status not in migrator.PROVISIONABLE:
        status = tenant.status if tenant is not None else "deleted"
        logger.warning("provision_tenant: skipping tenant %s — status %s is not NEW/PENDING.",
                       tenant_id, status)
        return {"tenant_id": tenant_id, "kind": "skipped", "reason": "not_provisionable",
                "status": status}

    res = migrator.migrate_tenant(tenant_id, migrator.RunOptions())
    out = {"tenant_id": tenant_id, "schema": res.schema, "kind": res.kind,
           "reason": res.reason, "msg": res.msg}
    if res.kind == "skipped" and res.reason in _RETRY_REASONS:
        logger.info("provision_tenant: %s %s (%s); retry %d/%d in %ds", res.schema, res.reason,
                    res.msg, self.request.retries + 1, PROVISION_MAX_RETRIES,
                    PROVISION_RETRY_COUNTDOWN)
        raise self.retry(countdown=PROVISION_RETRY_COUNTDOWN, max_retries=PROVISION_MAX_RETRIES)
    if res.kind == "failed":
        logger.error("provision_tenant: %s failed (%s): %s", res.schema, res.reason, res.msg)
    return out


@shared_task(queue=task_queue("service"), acks_late=True, max_retries=0)
def drop_tenant_schema_task(database: str, schema: str) -> dict[str, Any]:
    """Drop an orphaned tenant schema on `database` (shard alias).

    Enqueued by the tenant DELETE flow when the operator ticks "also drop the
    schema". The Tenant row is already gone by the time this runs, so
    drop_tenant_schema's live-tenant guard passes. Runs on the `service` queue.
    """
    logger.info("drop_tenant_schema_task: dropping %r on shard %r", schema, database)
    call_command("drop_tenant_schema", database=database, schema=schema, no_input=True)
    return {"database": database, "schema": schema, "dropped": True}


# Tenant-resolve gate reconcile: rebuild the treg:hosts SET + warm positive snapshots
# from the DB, single-writer (treg:warming lock lives inside run_locked). Tenant-agnostic
# (public context) → plain Task. Enqueued on-demand (host_registry.trigger_warm) and,
# in production, scheduled daily as a safety net. No-op unless TENANT_REGISTRY["WARM_ENABLED"].
#
# QUEUE = `fast`, deliberately — NOT `service` like its provisioning siblings above.
# Time-to-warm is on the AVAILABILITY path: while treg:hosts is absent, host_registry.check
# returns UNKNOWN → the resolver fails open under fill_cap → exhausting that budget answers
# legitimate tenants with a retryable 503 (ResolveDeferred). `service` is shared with
# provision_tenant, whose migrate_schemas run takes MINUTES, so a reconcile queued behind one
# would prolong exactly the outage it exists to end. `fast` drains fastest, and the cost is
# bounded by design rather than by the queue: cluster-wide there is at most ONE real run (the
# fenced treg:warming lock; every other delivery pings + fails to acquire and returns in ~2
# round-trips) and at most one enqueue per TENANT_REGISTRY["WARM_PENDING_SECONDS"] (the
# treg:warm_pending NX marker in trigger_warm). Spelled out via task_queue() rather than left
# to CELERY_TASK_DEFAULT_QUEUE so the choice is visible — it reads like an omission otherwise.
@shared_task(base=Task, queue=task_queue("fast"), acks_late=True, max_retries=0)
def reconcile_host_registry_task() -> dict[str, Any]:
    from tenants.resolver import host_registry
    return {"reconciled": host_registry.run_locked()}


# Register the fan-out dispatch tasks (fanout_dispatch / sub_dispatch). They live in a
# dedicated module for cohesion; importing it here means Celery autodiscover (which imports
# `tenants.tasks`) also registers them. Import LAST — dispatch imports tenants.models, so it
# must run after the app registry is ready (autodiscover fires post django.setup()).
from tenants.celery import dispatch  # noqa: E402,F401
