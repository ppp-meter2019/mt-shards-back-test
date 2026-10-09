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
from kombu.exceptions import OperationalError
from django.utils import timezone
from redis.exceptions import LockError, RedisError

from commons.platform.beat import FANOUT_TASK_NAME, beat_conf, task_queue
from commons.platform.redis_client import django_redis_raw_client
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


def _wave_lock(task_name: str, args_sig: str) -> Any:
    """The overlap lock for one wave of (task, args), unacquired.

    redis-py's Lock, not cache.add: `add` is SETNX+TTL with no way to release only-if-ours, so
    the previous implementation never released at all. The lock then lived its full TTL
    regardless of how long the wave took, which turned an overlap lock into a rate limiter —
    any interval entry shorter than LOCK_SECONDS silently fanned out once per LOCK_SECONDS,
    and both documented examples (5s and 30s) were capped at 60.

    Releasing is fenced: Lock.release() runs a Lua compare-and-delete against the token
    acquire() minted, so a wave that outlived its TTL cannot delete the lock a LATER wave now
    holds — which would let a third start on top of two and cascade. Same construct as
    tenants/resolver/registry.py::run_locked; two fenced locks in one codebase should look
    alike.

    TTL keeps one job only: the crash backstop. A worker killed mid-wave never reaches the
    finally, and LOCK_SECONDS bounds how long that blocks the schedule.

    Keyed on the SIGNATURE rather than the raw args so the lock and the TaskRun watermark use
    one value computed once per wave — they must never disagree about what counts as "the same
    schedule entry". The beat_lock alias is the BROKER Redis (noeviction): an app cache on
    allkeys-lru could simply evict a lock.
    """
    key = "beat:fanout:%s:%s" % (task_name, args_sig)
    client = django_redis_raw_client("beat_lock")
    return client.lock(key, timeout=beat_conf("LOCK_SECONDS"))


def _due_by_tenant_tz(task_name: str, args_sig: str, cron: str, now: "datetime",
                      grace: float) -> list[str]:
    """Calendar due-check per tenant, evaluated in each tenant's own timezone
    (level-triggered). Considers ONLY the latest past occurrence of `cron` and fires it
    iff it is (a) newer than that tenant's last run (TaskRun) and (b) within `grace` of
    now. A missed tick self-heals within grace; beyond grace it is skipped (never fired
    late, never N times). See deploy/celery_fanout_design.md §3 / §3.1.

    That skip is WARNED about once per dropped occurrence (bounded to a grace-wide band), so
    "the report stopped arriving for tenant Y" has a log line behind it instead of only a
    TaskRun row that quietly stopped advancing.

    Keyed by (task_name, args_sig), the SAME identity the overlap-lock uses: two entries
    sharing a task name but differing by args are independent schedules, and reading one
    watermark for both would let the wave that lands second treat the occurrence as already
    run and skip it indefinitely.
    """
    from datetime import datetime, timezone as _utc
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from croniter import croniter

    last = TaskRun.load_map(task_name, args_sig)
    due, dropped = [], []
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
        elapsed = (now - t_fire).total_seconds()
        if last_run is not None and t_fire <= last_run:
            continue                                # already dispatched for this occurrence
        if elapsed <= grace:
            due.append(schema)
        elif last_run is not None and elapsed <= 2 * grace:
            # Past grace and never dispatched: this occurrence is being DROPPED. That is the
            # documented behaviour (§3.1), but until now it was INVISIBLE — the only trace was
            # a TaskRun row quietly not advancing, plus a smaller number in _fan_out's INFO
            # line, which attributes nothing to anyone. Two guards keep this from becoming the
            # flood a bare `too_late` would be:
            #   last_run is not None  a tenant that has never run this entry would report a
            #                         miss for every past occurrence the moment the entry is
            #                         first added to the schedule (the case pinned by
            #                         test_no_retroactive_fire_on_deploy);
            #   elapsed <= 2 * grace  t_fire does not move until the NEXT occurrence, so with
            #                         no upper bound one missed daily task would log on every
            #                         tick for the rest of the day.
            # The band is one grace wide => at most grace/fanout_period lines per dropped
            # occurrence (5 at defaults) — the same ceiling as the duplicate-dispatch window.
            dropped.append(schema)
    if dropped:
        logger.warning(
            "tz-fanout %s: %d tenant(s) passed the %.0fs grace with no dispatch — "
            "occurrence DROPPED (e.g. %s)",
            task_name, len(dropped), grace, ", ".join(sorted(dropped)[:5]))
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
    lock = _wave_lock(task_name, args_sig)
    if not lock.acquire(blocking=False):
        logger.info("fanout_dispatch: %s skipped (overlapping wave)", task_name)
        return {"skipped": "overlapping"}
    try:
        return _fan_out(task_name, scope, cron, grace, task_args, task_kwargs,
                        task_options, batch_size, args_sig)
    finally:
        # Both arms swallow deliberately. By the time this runs the wave has ALREADY dispatched
        # every sub_dispatch, so an exception escaping here would discard a successful result
        # and report fanout as broken — see the ORDER: `return _fan_out(...)` evaluates the
        # body in full, stashes the value, and only then runs this block. Narrow on purpose:
        # LockError is a subclass of RedisError, so its arm comes first, and anything that is
        # neither is a bug in our own code and must still crash.
        try:
            lock.release()
        except LockError:
            # The wave outlived LOCK_SECONDS, so a LATER wave may already hold this key and it
            # is not ours to delete. Releasing regardless would take that wave's lock and let a
            # third start — a cascade of concurrent waves, the exact failure the lock prevents.
            # Worth a warning: it means dispatch is slower than its own lock TTL.
            logger.warning("fanout_dispatch: %s lock expired before release", task_name,
                           exc_info=True)
        except RedisError:
            # Redis went away between acquire and release — release() runs a Lua script, so it
            # is a network call. The key expires on its own via LOCK_SECONDS; the cost is one
            # wave's worth of delay, not a lost dispatch. ERROR rather than WARNING: a broker
            # that drops mid-wave is an outage, not a slow tick.
            logger.error("fanout_dispatch: %s could not release its lock (redis unreachable)",
                         task_name, exc_info=True)


def _fan_out(task_name: str, scope: str, cron: str | None, grace: float | None,
             task_args: Sequence[Any] | None, task_kwargs: dict[str, Any] | None,
             task_options: dict[str, Any] | None, batch_size: int | None,
             args_sig: str) -> dict[str, Any]:
    """The wave itself, extracted so fanout_dispatch is just lock / body / fenced release."""
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
    # `options` is a legitimate part of a beat entry and the fanout design promises to carry it
    # through, so it may legally contain `headers` — which used to COLLIDE with ours:
    # send_task(..., headers={...}, **options) raised "got multiple values for keyword argument
    # 'headers'". That TypeError was deterministic, so it hit every schema on every tick while
    # the loop below logged it as transient; fanout_dispatch still reported a clean wave, and
    # for calendar entries the watermark never advanced, so the occurrence retried until grace
    # expired and then vanished. Merge instead, with OUR key stamped LAST so a schedule entry
    # cannot override the schema the task will run in.
    options = dict(task_options or {})
    caller_headers = options.pop("headers", None) or {}
    sent = []
    for schema in schemas:
        try:
            current_app.send_task(
                task_name, args=task_args or [], kwargs=task_kwargs or {},
                headers={**caller_headers, "_schema_name": schema}, **options,
            )
            sent.append(schema)
        except OperationalError:
            # Broker unreachable / connection lost: genuinely transient, and the next tick is
            # the right retry. The only exception class this loop may swallow.
            logger.exception("sub_dispatch: send failed for schema %r "
                             "(will be retried next tick)", schema)
        except Exception:
            # Anything else is deterministic — a bad `options` key, a serialisation failure —
            # and will fail identically on every schema and every tick. Re-raise so the task is
            # marked FAILED instead of reporting a wave that silently delivered nothing.
            logger.exception("sub_dispatch: %s cannot be sent (not retryable)", task_name)
            raise
    # Calendar tasks only: advance the watermark for successfully-sent schemas
    # (at-least-once — mark AFTER send). Interval tasks pass run_ts=None.
    if run_ts and sent:
        TaskRun.mark_ran(task_name, args_sig, sent, run_ts)
    return {"task": task_name, "sent": len(sent), "requested": len(schemas)}
