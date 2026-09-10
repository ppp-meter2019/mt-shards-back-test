"""Tenant provisioning / housekeeping tasks.

Queue placement — MT-only; task_queue() returns None in standalone, so every task below
falls back to the host's default queue there:
  provision_tenant, drop_tenant_schema_task  -> `service`  management operations, not
      business tasks, and long-running (provisioning runs migrate_schemas).
  reconcile_host_registry_task               -> `fast`     NOT `service`: time-to-warm is
      on the availability path, and the cost is bounded by the treg:warming lock rather
      than by the queue. Full rationale at its decorator.

provision_tenant is the async equivalent of
`migrate_schemas --tenant --schema_name=<schema>`: create the schema on the tenant's
shard, migrate it, flip NEW->ACTIVE/FAILED.

Re-provisioning guard: provisioning only runs on a NEW tenant. Any other status
(PENDING in progress, ACTIVE/DEACTIVATED already provisioned, FAILED needs a reset via
reconcile_tenants) is skipped. The real concurrency guard is migrate_schemas' atomic
NEW->PENDING claim (UPDATE ... WHERE status='new'); this status check is a cheap early-out.

The generic fan-out scheduler infrastructure (fanout_dispatch / sub_dispatch / the tz
due-check) lives in tenants/celery/dispatch.py — imported at the bottom of THIS module so
Celery autodiscover (which imports `<app>.tasks`) registers those tasks too.
"""
from celery import Task, shared_task
from celery.utils.log import get_task_logger
from django.core.management import call_command

from commons.platform.beat import task_queue

from .models import Tenant

logger = get_task_logger(__name__)


@shared_task(bind=True, queue=task_queue("service"), acks_late=True, max_retries=0)
def provision_tenant(self, tenant_id):
    tenant = Tenant.objects.select_related("shard").get(pk=tenant_id)

    if tenant.status != Tenant.Status.NEW:
        logger.warning(
            "provision_tenant: skipping %s — status is %s, not NEW (already "
            "provisioned / in progress / failed).",
            tenant.schema_name, tenant.status,
        )
        return {"schema": tenant.schema_name, "status": tenant.status, "skipped": True}

    logger.info("Provisioning %s on shard %s", tenant.schema_name, tenant.shard.alias)
    # migrate_schemas owns the status machine: NEW->PENDING claim, CREATE SCHEMA,
    # migrate, finalize NEW->ACTIVE (or FAILED + last_error).
    call_command("migrate_schemas", tenant=True, schema_name=tenant.schema_name)

    tenant.refresh_from_db()
    logger.info("Provision finished: %s -> %s", tenant.schema_name, tenant.status)
    return {"schema": tenant.schema_name, "status": tenant.status, "skipped": False}


@shared_task(queue=task_queue("service"), acks_late=True, max_retries=0)
def drop_tenant_schema_task(database, schema):
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
def reconcile_host_registry_task():
    from tenants.resolver import host_registry
    return {"reconciled": host_registry.run_locked()}


# Register the fan-out dispatch tasks (fanout_dispatch / sub_dispatch). They live in a
# dedicated module for cohesion; importing it here means Celery autodiscover (which imports
# `tenants.tasks`) also registers them. Import LAST — dispatch imports tenants.models, so it
# must run after the app registry is ready (autodiscover fires post django.setup()).
from tenants.celery import dispatch  # noqa: E402,F401
