"""Tenant-aware in-memory Celery Beat schedulers — multi-DB adaptation of
tenant_schemas_celery.scheduler. A periodic task is fanned out per tenant; the
only change is that schema_context is OUR shard-aware version, so each enqueue
captures the tenant's shard.
"""
import copy
import logging

from celery.beat import PersistentScheduler, ScheduleEntry, Scheduler
from django.db import models

from .compat import get_public_schema_name, get_tenant_model, schema_context

logger = logging.getLogger(__name__)
Tenant = get_tenant_model()


class TenantAwareScheduleEntry(ScheduleEntry):
    def __init__(self, *args, **kwargs):
        if args and len(args) == 9:
            args = args[:-1]                       # unpickled: drop legacy tenant_schemas
        else:
            kwargs.pop("tenant_schemas", None)
        super().__init__(*args, **kwargs)

    def __reduce__(self):
        return self.__class__, (
            self.name, self.task, self.last_run_at, self.total_run_count,
            self.schedule, self.args, self.kwargs, self.options,
        )


class TenantAwareSchedulerMixin:
    @classmethod
    def get_queryset(cls) -> models.QuerySet:
        return Tenant.objects.all()

    def _tenant_aware_beat_schedule_to_dict(self, beat_schedule):
        result = {}
        for name, entry in copy.deepcopy(beat_schedule).items():
            tenant_schemas = entry.pop("tenant_schemas", None)
            if tenant_schemas is None:
                entry.setdefault("options", {}).setdefault("headers", {})["_all_tenants_only"] = True
                result[f"{name}@__all_tenants_only__"] = entry
            else:
                for schema_name in tenant_schemas:
                    entry.setdefault("options", {}).setdefault("headers", {})["_schema_name"] = schema_name
                    result[f"{name}@{schema_name}"] = copy.deepcopy(entry)
        return result

    def apply_entry(self, entry, producer=None):
        tenants = self.get_queryset()
        all_only = entry.options.setdefault("headers", {}).get("_all_tenants_only")
        if all_only:
            schemas = list(tenants.exclude(schema_name=get_public_schema_name())
                                  .values_list("schema_name", flat=True))
        else:
            schemas = list(tenants.filter(schema_name=entry.options["headers"]["_schema_name"])
                                  .values_list("schema_name", flat=True))
        logger.info("TenantAwareScheduler: due %s -> %s tenants",
                    entry.name, "all" if all_only else len(schemas))
        for schema in schemas:
            with schema_context(schema):               # shard-aware → enqueue carries the shard
                try:
                    self.apply_async(entry, producer=producer, advance=False)
                except Exception as exc:
                    logger.exception(exc)


class TenantAwareScheduler(TenantAwareSchedulerMixin, Scheduler):
    Entry = TenantAwareScheduleEntry

    def merge_inplace(self, b):
        return super().merge_inplace(self._tenant_aware_beat_schedule_to_dict(b))


class TenantAwarePersistentScheduler(TenantAwareSchedulerMixin, PersistentScheduler):
    Entry = TenantAwareScheduleEntry

    def merge_inplace(self, b):
        return super().merge_inplace(self._tenant_aware_beat_schedule_to_dict(b))
