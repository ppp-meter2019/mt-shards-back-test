"""Fanout dispatch — the producer for tenant-scoped scheduled tasks (MT-only).

Beat fires ONE fanout_dispatch per due entry (see commons.platform.beat.scoped_schedule); it
splits the target tenants into batches handed to sub_dispatch, which run in parallel on the
`fanout` queue and enqueue the REAL task per tenant with the `_schema_name` header (the
existing worker-side TenantTask.__call__ then runs it on that shard/schema). Full design:
deploy/celery_fanout_design.md.

Registered via tenants/tasks.py (which Celery autodiscover imports) — see the import there.
The task NAMES are kept stable (`tenants.tasks.fanout_dispatch` / `.sub_dispatch`) via
`name=`, so the string contract in scoped_schedule and the tenants.E002 check does not
depend on this module's path.
"""
import hashlib
import json

from celery import Task, current_app, shared_task
from celery.utils.log import get_task_logger
from django.core.cache import caches
from django.utils import timezone

from commons.platform.beat import FANOUT_TASK_NAME, beat_conf, task_queue
from commons.platform.tenancy import active_target_schemas, active_tenants_with_tz
from tenants.models import TaskRun

logger = get_task_logger(__name__)


def _argsig(task_args):
    """Stable short signature of the task args, so two schedule entries that share a
    task name but differ by args (e.g. fetch(1) vs fetch(7)) get DISTINCT locks."""
    blob = json.dumps(task_args or [], sort_keys=True, default=str)
    # usedforsecurity=False: this digest is only a cache-key discriminator, never a security
    # boundary — the flag keeps md5 usable under a FIPS-enabled OpenSSL (where a bare
    # hashlib.md5() would raise) and is a no-op elsewhere.
    return hashlib.md5(blob.encode(), usedforsecurity=False).hexdigest()[:12]


def _acquire_lock(task_name, task_args):
    """Overlap-lock (atomic cache.add == SETNX). Returns True if this wave may run; False if a
    previous wave of the SAME (task, args) still holds the lock (a deliberate skip). If the
    beat_lock Redis is DOWN, cache.add RAISES (IGNORE_EXCEPTIONS is off) and fanout_dispatch
    fails LOUDLY — surfacing the outage instead of silently skipping."""
    key = "beat:fanout:%s:%s" % (task_name, _argsig(task_args))
    return bool(caches["beat_lock"].add(key, "1", timeout=beat_conf("LOCK_SECONDS")))


def _due_by_tenant_tz(task_name, task_args, cron, now, grace):
    """Calendar due-check per tenant, evaluated in each tenant's own timezone
    (level-triggered). Considers ONLY the latest past occurrence of `cron` and fires it
    iff it is (a) newer than that tenant's last run (TaskRun) and (b) within `grace` of
    now. A missed tick self-heals within grace; beyond grace it is skipped (never fired
    late, never N times). See deploy/celery_fanout_design.md §3 / §3.1.
    """
    from datetime import datetime, timezone as _utc
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from croniter import croniter

    last = TaskRun.load_map(task_name)
    due = []
    for schema, tzname in active_tenants_with_tz():
        try:
            tz = ZoneInfo(tzname)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("tz-fanout %s: invalid timezone %r for %s — skipped",
                           task_name, tzname, schema)
            continue
        # get_prev is exclusive of `now`; ticks land just AFTER the scheduled second, so
        # the current occurrence is returned. (An exact-boundary now would be caught next tick.)
        t_fire = croniter(cron, now.astimezone(tz)).get_prev(datetime).astimezone(_utc.utc)
        last_run = last.get(schema)
        already = last_run is not None and t_fire <= last_run
        too_late = (now - t_fire).total_seconds() > grace
        if not already and not too_late:
            due.append(schema)
    return due


@shared_task(name=FANOUT_TASK_NAME, base=Task,
             queue=task_queue("fanout"), acks_late=True, max_retries=0)
def fanout_dispatch(task_name, scope="tenants", cron=None, grace=None,
                    task_args=None, task_kwargs=None, task_options=None, batch_size=None):
    if not _acquire_lock(task_name, task_args):
        logger.info("fanout_dispatch: %s skipped (overlapping wave)", task_name)
        return {"skipped": "overlapping"}

    batch_size = batch_size or beat_conf("BATCH_SIZE")
    if cron:                                    # calendar → per-tenant tz due-check
        now = timezone.now()
        grace = grace if grace is not None else beat_conf("TZ_GRACE_SECONDS")
        due = _due_by_tenant_tz(task_name, task_args, cron, now, grace)
        run_ts = now.isoformat()                # watermark; sub_dispatch stamps it after send
    else:                                       # interval → all ACTIVE tenants (tz-agnostic)
        due = list(active_target_schemas(scope))
        run_ts = None
    for i in range(0, len(due), batch_size):
        sub_dispatch.delay(task_name, due[i:i + batch_size],
                           task_args, task_kwargs, task_options, run_ts)
    logger.info("fanout_dispatch: %s -> %d tenant(s) in %d batch(es) (%s)",
                task_name, len(due), (len(due) + batch_size - 1) // batch_size,
                "tz" if cron else "interval")
    return {"task": task_name, "scope": scope, "fanned_out": len(due)}


@shared_task(name="tenants.tasks.sub_dispatch", base=Task,
             queue=task_queue("fanout"), acks_late=True, max_retries=0)
def sub_dispatch(task_name, schemas, task_args=None, task_kwargs=None,
                 task_options=None, run_ts=None):
    sent = []
    for schema in schemas:
        try:
            current_app.send_task(
                task_name, args=task_args or [], kwargs=task_kwargs or {},
                headers={"_schema_name": schema}, **(task_options or {}),
            )
            sent.append(schema)
        except Exception:
            logger.exception("sub_dispatch: send failed for schema %r "
                             "(will be retried next tick)", schema)
    # Calendar tasks only: advance the watermark for successfully-sent schemas
    # (at-least-once — mark AFTER send). Interval tasks pass run_ts=None.
    if run_ts and sent:
        TaskRun.mark_ran(task_name, sent, run_ts)
    return {"task": task_name, "sent": len(sent), "requested": len(schemas)}
