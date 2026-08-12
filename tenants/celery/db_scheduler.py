"""Tenant-aware django-celery-beat (DB-backed) scheduler — multi-DB adaptation
of tenant_schemas_celery.db_scheduler. Enumerates PeriodicTask across public +
every tenant schema; schema_context/tenant_context are OUR shard-aware versions,
so per-schema reads route to the right shard.
"""
import json
import logging

from django.db import Error as DBError
from django.db.utils import ConnectionDoesNotExist
from django_celery_beat.schedulers import DatabaseScheduler, ModelEntry

from .compat import get_public_schema_name, get_tenant_model, schema_context
from .scheduler import TenantAwareSchedulerMixin

logger = logging.getLogger(__name__)

# Built-in global periodic tasks that clean a GLOBAL store (the result backend),
# not per-tenant data → run once, don't fan out per schema. Per analysis this is
# the only auto-installed beat task (canvas primitives like chord_unlock are NOT
# beat-scheduled). Explicit allowlist (not a "celery." prefix) so a user task is
# never silently deduped.
_GLOBAL_TASKS = frozenset({"celery.backend_cleanup"})


def _task_schema(options):
    return options.get("headers", {}).get("_schema_name", get_public_schema_name())


class TenantAwareModelEntry(ModelEntry):
    def __init__(self, model, **kwargs):
        super().__init__(model, **kwargs)
        # Namespace the SCHEDULE-ENTRY identity by schema so same-named per-tenant
        # tasks coexist in the merged schedule and each fires for its own tenant.
        # The DB model.name is left untouched (save() writes by pk, not name).
        # Global built-ins keep their plain name (deduped to one copy in enabled_models).
        if model.name not in _GLOBAL_TASKS:
            self.name = f"{model.name}@{_task_schema(self.options)}"

    def is_due(self):
        with schema_context(_task_schema(self.options)):
            return super().is_due()

    def save(self):
        with schema_context(_task_schema(self.options)):
            super().save()


class TenantAwarePeriodicTasks:
    """Change-detection sentinel. Reads ONE Redis marker per schema (bumped by an
    ORM signal on any schedule change; see change_marker.py) via a single MGET —
    no per-tenant DB round-trip. Direct-SQL injections bypass the signal; use
    `manage.py resync_beat_schedules`."""

    @classmethod
    def last_change(cls):
        from datetime import datetime, timezone as _tz

        from .change_marker import marker_max

        ts = marker_max()
        return datetime.fromtimestamp(ts, tz=_tz.utc) if ts else None


class TenantAwareDatabaseScheduler(TenantAwareSchedulerMixin, DatabaseScheduler):
    Entry = TenantAwareModelEntry
    Changes = TenantAwarePeriodicTasks

    def setup_schedule(self):
        self.install_default_entries(self.schedule)
        self.update_from_dict(
            self._tenant_aware_beat_schedule_to_dict(self.app.conf.beat_schedule)
        )
        # Seed the baseline from current markers so the first tick after startup
        # doesn't trigger a redundant full reload (we just loaded everything).
        self._last_timestamp = self.Changes.last_change()

    def get_public_schema_name(self):
        return [get_public_schema_name()]

    def get_tenant_schema_names(self, exclude):
        # ONLY ACTIVE tenants: their schema is provisioned + migrated. NEW/PENDING have
        # no schema/tables yet (a restart before provisioning would otherwise crash beat
        # with "relation does not exist"); FAILED may be half-migrated; DEACTIVATED is
        # intentionally closed (don't schedule its tasks). A tenant returning to ACTIVE
        # is re-included on the next reload — see the marker bump on status transitions.
        model = get_tenant_model()
        return list(model.objects
                    .filter(status=model.Status.ACTIVE)
                    .exclude(schema_name__in=exclude)
                    .values_list("schema_name", flat=True))

    def get_schema_names(self):
        public = self.get_public_schema_name()
        return [*public, *self.get_tenant_schema_names(public)]

    def enabled_models(self):
        # Collect enabled PeriodicTasks across public + every tenant schema, stamping
        # each with its schema (headers._schema_name). We do NOT mutate task.name —
        # per-schema namespacing of the schedule-entry identity happens in
        # TenantAwareModelEntry + all_as_schedule, so model.name stays truthful.
        models_, seen_global = [], set()
        for schema_name in self.get_schema_names():
            # Per-schema resilience: one unreachable shard / broken schema must NOT
            # take down the whole beat process — skip it THIS cycle and log. It is
            # re-tried on the next reload. (Status filtering already excludes
            # unprovisioned tenants; this is the safety net for transient failures.)
            try:
                with schema_context(schema_name):
                    # select_related the schedule FKs so they are loaded HERE, inside
                    # the tenant's schema. Otherwise ModelEntry (built later in
                    # all_as_schedule, OUTSIDE this context) would lazily fetch
                    # crontab/interval/... against the wrong schema (public/default)
                    # → wrong schedule or DoesNotExist for tenant tasks.
                    tasks = list(
                        super().enabled_models_qs()
                        .select_related("interval", "crontab", "solar", "clocked")
                    )
            except (DBError, ConnectionDoesNotExist):
                logger.warning(
                    "beat: skipping schema %r this cycle — schedule unreadable "
                    "(shard down / schema not ready)", schema_name, exc_info=True,
                )
                continue
            for task in tasks:
                if task.name in _GLOBAL_TASKS:
                    # Global built-in (e.g. celery.backend_cleanup): auto-installed
                    # into every schema but cleans a GLOBAL store — keep one copy.
                    if task.name in seen_global:
                        continue
                    seen_global.add(task.name)
                headers = json.loads(task.headers)
                headers.setdefault("_schema_name", schema_name)
                task.headers = json.dumps(headers)
                models_.append(task)
        return models_

    def all_as_schedule(self):
        # Key the merged schedule by the (schema-namespaced) entry.name instead of
        # model.name, so same-named per-tenant tasks don't overwrite each other.
        # entry.name MUST equal the dict key — sync()/_dirty look entries up by name.
        s = {}
        for model in self.enabled_models():
            try:
                entry = self.Entry(model, app=self.app)
                s[entry.name] = entry
            except ValueError:
                pass
        return s
