"""Tenant-aware django-celery-beat (DB-backed) scheduler — multi-DB adaptation
of tenant_schemas_celery.db_scheduler. Enumerates PeriodicTask across public +
every tenant schema; schema_context/tenant_context are OUR shard-aware versions,
so per-schema reads route to the right shard.
"""
import json
import logging

from django_celery_beat.models import PeriodicTask, PeriodicTasks
from django_celery_beat.schedulers import DatabaseScheduler, ModelEntry

from .compat import (
    get_public_schema_name, get_tenant_model, schema_context, tenant_context,
)
from .scheduler import TenantAwareSchedulerMixin

logger = logging.getLogger(__name__)


def _task_schema(options):
    return options.get("headers", {}).get("_schema_name", get_public_schema_name())


class TenantAwareModelEntry(ModelEntry):
    def is_due(self):
        with schema_context(_task_schema(self.options)):
            return super().is_due()

    def save(self):
        with schema_context(_task_schema(self.options)):
            super().save()


class TenantAwarePeriodicTasks:
    @classmethod
    def last_change(cls):
        with schema_context(get_public_schema_name()):
            all_tenants = list(get_tenant_model().objects.all())
            last_change = PeriodicTasks.last_change()
        for tenant in all_tenants:
            with tenant_context(tenant):
                tlc = PeriodicTasks.last_change()
                last_change = (max(last_change, tlc) if last_change and tlc
                               else (last_change or tlc))
        return last_change


class TenantAwareDatabaseScheduler(TenantAwareSchedulerMixin, DatabaseScheduler):
    Entry = TenantAwareModelEntry
    Changes = TenantAwarePeriodicTasks

    def setup_schedule(self):
        self.install_default_entries(self.schedule)
        self.update_from_dict(
            self._tenant_aware_beat_schedule_to_dict(self.app.conf.beat_schedule)
        )

    def get_public_schema_name(self):
        return [get_public_schema_name()]

    def get_tenant_schema_names(self, exclude):
        return list(get_tenant_model().objects.exclude(schema_name__in=exclude)
                    .values_list("schema_name", flat=True))

    def get_schema_names(self):
        public = self.get_public_schema_name()
        return [*public, *self.get_tenant_schema_names(public)]

    def enabled_models(self):
        models_, seen = [], {}
        for schema_name in self.get_schema_names():
            with schema_context(schema_name):
                for task in super().enabled_models_qs():
                    if prev := seen.get(task.name):
                        raise ValueError(
                            f"duplicate periodic task name {task.name!r}; seen in {prev!r}")
                    headers = json.loads(task.headers)
                    headers.setdefault("_schema_name", schema_name)
                    task.headers = json.dumps(headers)
                    models_.append(task)
                    seen[task.name] = schema_name
        return models_
