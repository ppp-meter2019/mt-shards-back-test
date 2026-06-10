"""Tenant background tasks (first consumer of the shard-aware Celery layer).

provision_tenant runs on the `service` queue (it's a management/housekeeping
operation, not a business task). It is the async equivalent of
`migrate_schemas --tenant --schema_name=<schema>`:
create the schema on the tenant's shard, migrate it, flip NEW->ACTIVE/FAILED.

Re-provisioning guard: provisioning only runs on a NEW tenant. Any other status
(PENDING in progress, ACTIVE/DEACTIVATED already provisioned, FAILED needs a
reset via reconcile_tenants) is skipped — you cannot re-provision an
already-provisioned tenant. The real concurrency guard is migrate_schemas'
atomic NEW->PENDING claim (UPDATE ... WHERE status='new'); this status check is
a cheap early-out, and the view rejects ineligible statuses up front.
"""
from celery import shared_task
from celery.utils.log import get_task_logger
from django.core.management import call_command

from .models import Tenant

logger = get_task_logger(__name__)


@shared_task(bind=True, acks_late=True, max_retries=0)
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
