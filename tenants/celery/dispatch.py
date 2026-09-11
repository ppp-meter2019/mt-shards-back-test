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
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from celery import Task, current_app, shared_task
from celery.utils.log import get_task_logger
from django.core.cache import caches
from django.utils import timezone

from commons.platform.beat import FANOUT_TASK_NAME, beat_conf, task_queue
from commons.platform.tenancy import active_target_schemas, active_tenants_with_tz
from tenants.models import TaskRun

if TYPE_CHECKING:                       # annotation-only: the runtime import is deliberately
    from datetime import datetime       # local to _due_by_tenant_tz (cold path)

logger = get_task_logger(__name__)

# The module's PUBLIC surface. `argsig` is on it deliberately, not incidentally: the
# tenants.E006 system check (tenants/checks/beat.py) must compute the schedule-entry
# signature with the SAME code the runtime keys its overlap-lock and TaskRun watermark on,
# or the check and the runtime would disagree about what "the same entry" means. That makes
# it a cross-module contract, so it carries a public name — a leading underscore would have
# invited a rename that silently breaks the check.
__all__ = ["argsig", "fanout_dispatch", "sub_dispatch"]


def argsig(task_args: Sequence[Any] | None) -> str:
    """Stable short signature of the task args, so two schedule entries that share a
    task name but differ by args (e.g. fetch(1) vs fetch(7)) get DISTINCT locks and
    DISTINCT TaskRun watermarks.

    EMPTY args (None or []) map to the empty string, not to a digest of "[]". Most schedule
    entries take no args, so this keeps their lock key readable (`beat:fanout:x.report:`)
    and — more to the point — keeps their TaskRun rows readable, which is what an operator
    reads by hand when asking why a task did not fire for a tenant. A digest there would be
    a constant that never distinguishes anything. Uniqueness is unaffected: "" can never
    collide with a 12-hex-char digest.
    """
    if not task_args:
        return ""
    blob = json.dumps(task_args, sort_keys=True, default=str)
    # usedforsecurity=False: this digest is only a cache-key discriminator, never a security
    # boundary — the flag keeps md5 usable under a FIPS-enabled OpenSSL (where a bare
    # hashlib.md5() would raise) and is a no-op elsewhere.
    return hashlib.md5(blob.encode(), usedforsecurity=False).hexdigest()[:12]


def _acquire_lock(task_name: str, args_sig: str) -> bool:
    """Overlap-lock (atomic cache.add == SETNX). Returns True if this wave may run; False if a
    previous wave of the SAME (task, args) still holds the lock (a deliberate skip). If the
    beat_lock Redis is DOWN, cache.add RAISES (IGNORE_EXCEPTIONS is off) and fanout_dispatch
    fails LOUDLY — surfacing the outage instead of silently skipping.

    Takes the signature rather than the raw args so the lock and the TaskRun watermark are
    keyed by one value computed once per wave — they must never disagree about what counts
    as "the same schedule entry"."""
    key = "beat:fanout:%s:%s" % (task_name, args_sig)
    return bool(caches["beat_lock"].add(key, "1", timeout=beat_conf("LOCK_SECONDS")))


def _due_by_tenant_tz(task_name: str, args_sig: str, cron: str, now: "datetime",
                      grace: float) -> list[str]:
    """Calendar due-check per tenant, evaluated in each tenant's own timezone
    (level-triggered). Considers ONLY the latest past occurrence of `cron` and fires it
    iff it is (a) newer than that tenant's last run (TaskRun) and (b) within `grace` of
    now. A missed tick self-heals within grace; beyond grace it is skipped (never fired
    late, never N times). See deploy/celery_fanout_design.md §3 / §3.1.

    Keyed by (task_name, args_sig), the SAME identity the overlap-lock uses: two entries
    sharing a task name but differing by args are independent schedules, and reading one
    watermark for both would let the wave that lands second treat the occurrence as already
    run and skip it indefinitely.
    """
    from datetime import datetime, timezone as _utc
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from croniter import croniter

    last = TaskRun.load_map(task_name, args_sig)
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
def fanout_dispatch(task_name: str, scope: str = "tenants", cron: str | None = None,
                    grace: float | None = None, task_args: Sequence[Any] | None = None,
                    task_kwargs: dict[str, Any] | None = None,
                    task_options: dict[str, Any] | None = None,
                    batch_size: int | None = None) -> dict[str, Any]:
    # ONE signature per wave, shared by the lock, the due-check and the watermark. Computing
    # it in each place instead would make three copies of "which schedule entry is this".
    args_sig = argsig(task_args)
    if not _acquire_lock(task_name, args_sig):
        logger.info("fanout_dispatch: %s skipped (overlapping wave)", task_name)
        return {"skipped": "overlapping"}

    batch_size = batch_size or beat_conf("BATCH_SIZE")
    if cron:                                    # calendar → per-tenant tz due-check
        now = timezone.now()
        grace = grace if grace is not None else beat_conf("TZ_GRACE_SECONDS")
        due = _due_by_tenant_tz(task_name, args_sig, cron, now, grace)
        run_ts = now.isoformat()                # watermark; sub_dispatch stamps it after send
    else:                                       # interval → all ACTIVE tenants (tz-agnostic)
        due = list(active_target_schemas(scope))
        run_ts = None
    for i in range(0, len(due), batch_size):
        sub_dispatch.delay(task_name, due[i:i + batch_size],
                           task_args, task_kwargs, task_options, run_ts, args_sig)
    logger.info("fanout_dispatch: %s -> %d tenant(s) in %d batch(es) (%s)",
                task_name, len(due), (len(due) + batch_size - 1) // batch_size,
                "tz" if cron else "interval")
    return {"task": task_name, "scope": scope, "fanned_out": len(due)}


@shared_task(name="tenants.tasks.sub_dispatch", base=Task,
             queue=task_queue("fanout"), acks_late=True, max_retries=0)
def sub_dispatch(task_name: str, schemas: Sequence[str], task_args: Sequence[Any] | None = None,
                 task_kwargs: dict[str, Any] | None = None,
                 task_options: dict[str, Any] | None = None, run_ts: str | None = None,
                 args_sig: str | None = None) -> dict[str, Any]:
    # args_sig defaults to None so a message enqueued by a PREVIOUS release (before the
    # parameter existed) still runs instead of failing with a TypeError under acks_late +
    # max_retries=0. Recomputing locally is equivalent: task_args have already been through
    # the broker's JSON round-trip by the time either side sees them.
    if args_sig is None:
        args_sig = argsig(task_args)
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
        TaskRun.mark_ran(task_name, args_sig, sent, run_ts)
    return {"task": task_name, "sent": len(sent), "requested": len(schemas)}
