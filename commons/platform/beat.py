"""Mode-aware Celery helpers for application code: the fanout-dispatch schedule wrapper
(scoped_schedule) plus the task_queue() routing shim. task_queue lived in a sibling
celery.py, merged here so no module in this package shadows the real `celery` package.
Full design: deploy/celery_fanout_design.md.

`scoped_schedule(entry, scope=...)` wraps a normal Celery beat entry so that, under
multitenant, a per-tenant task is fanned out via `tenants.tasks.fanout_dispatch`
instead of running once. In standalone (or for `scope="public"`) it is the
IDENTITY — the entry runs on stock beat unchanged.

tz-awareness is derived from the schedule TYPE, never a manual flag:
  * crontab           -> calendar, per-tenant local time (fanned out with a `cron`
                         spec; beat fires the dispatcher every `fanout_period`);
  * number/timedelta/
    celery `schedule`  -> interval, tz-irrelevant, fanned out to all ACTIVE tenants
                         at that cadence;
  * anything else      -> ImproperlyConfigured (strings / solar / clocked rejected;
                         one-off runs go through apply_async(eta=...)).
"""
import numbers
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from celery.schedules import crontab, schedule as interval_schedule

from commons.platform.mode import use_multitenant, bootstrap_float

# Canonical name of the fan-out dispatcher task — the contract between the PRODUCER
# (scoped_schedule below, which emits it into the beat entry's "task") and the DEFINER
# (tenants.celery.dispatch, which registers it via @shared_task(name=...)). They live on
# opposite sides of the one-way commons<-tenants layer boundary and can't share the Celery
# registration, so this single string is the shared source of truth; `tenants` imports it UP.
# In standalone it is just an unused constant (scoped_schedule is identity there). The whole
# wiring is enforced live by the tenants.E005 system check.
FANOUT_TASK_NAME = "tenants.tasks.fanout_dispatch"

# Load-time default for the calendar (tz) beat tick. NOT a runtime TENANT_BEAT knob:
# scoped_schedule bakes it into the beat cadence during settings load (where it cannot read
# settings.TENANT_BEAT / settings_local.py — both come later). Override it code-free the SAME
# way as USE_MULTITENANT — env FANOUT_PERIOD_SECONDS → settings_mode.py → this default — or
# per-entry via scoped_schedule(fanout_period=…).
FANOUT_PERIOD_DEFAULT = bootstrap_float("FANOUT_PERIOD_SECONDS", 60.0)

# In-code defaults for the RUNTIME-overridable TENANT_BEAT knobs — resolved by beat_conf() at
# task runtime, so settings.TENANT_BEAT[key] wins over these. (The beat tick is deliberately NOT
# here; it is load-time only — see FANOUT_PERIOD_DEFAULT above.)
BEAT_DEFAULTS = {
    "TZ_GRACE_SECONDS": 300,   # max lateness for a missed calendar occurrence (self-heal bound)
    "BATCH_SIZE": 100,         # tenants per sub_dispatch batch
    "LOCK_SECONDS": 60,        # overlap-lock TTL for a dispatch wave
}


def beat_conf(key):
    """Resolve a TENANT_BEAT knob: settings.TENANT_BEAT[key] -> in-code default."""
    return getattr(settings, "TENANT_BEAT", {}).get(key, BEAT_DEFAULTS[key])


class SchedEntry(dict):
    """A beat-schedule entry that carries its scope as an ATTRIBUTE (not a dict key).

    Celery reads a schedule entry as a plain mapping — `ScheduleEntry(**entry)` unpacks only
    the dict ITEMS — so `scope` is invisible to Celery (no unknown-key error), while
    `scope_of()` reads it via getattr. The scope travels WITH the object (survives
    copy/deepcopy), so there is no out-of-band registry and no dependence on object id().
    NB: an explicit `{**entry}` / `dict(entry)` cast drops back to a plain dict (losing the
    attribute) — store the SchedEntry directly, don't spread it.
    """


def scope_of(entry):
    """The scope (`"public"`/`"tenants"`) scoped_schedule wrapped this entry with, or None if
    the entry was NOT produced by scoped_schedule (a raw dict that bypassed the helper).
    Used by the tenants.E003 system check to enforce that every entry is wrapped."""
    return getattr(entry, "scope", None)


def _crontab_to_cronspec(c):
    """celery crontab -> 5-field cron string, from its original (unparsed) fields."""
    return "{0} {1} {2} {3} {4}".format(
        c._orig_minute, c._orig_hour, c._orig_day_of_month,
        c._orig_month_of_year, c._orig_day_of_week,
    )


def _classify_schedule(sched):
    """-> (kind, beat_schedule_value, cron_spec_or_None). Fail-fast on unsupported."""
    if isinstance(sched, (numbers.Number, timedelta, interval_schedule)):
        return "interval", sched, None
    if isinstance(sched, crontab):
        return "calendar", sched, _crontab_to_cronspec(sched)
    raise ImproperlyConfigured(
        f"scoped_schedule: unsupported schedule {type(sched).__name__}. Allowed: number / "
        f"timedelta / celery.schedules.schedule (interval) or crontab (calendar). "
        f"Strings / solar / clocked are rejected; one-off runs use apply_async(eta=...)."
    )


def scoped_schedule(entry, *, scope="tenants", fanout_period=None, grace=None):
    """Wrap a beat entry for the fanout model. Identity in standalone / for public."""
    if scope not in ("public", "tenants"):
        raise ImproperlyConfigured(f"scoped_schedule: scope must be 'public' or 'tenants', got {scope!r}")

    # use_multitenant() (NOT settings.USE_MULTITENANT): scoped_schedule runs at settings-load
    # in the host project, where touching django.conf.settings would cache incomplete settings.
    if not use_multitenant() or scope == "public":
        out = SchedEntry(entry)                         # passthrough -> stock beat (scope on attr)
        out.scope = scope
        return out

    kind, sched, cron = _classify_schedule(entry["schedule"])
    disp = {
        "task_name": entry["task"],
        "scope": scope,
        "task_args": list(entry.get("args", ()) or ()),
        "task_kwargs": entry.get("kwargs", {}) or {},
        "task_options": entry.get("options", {}) or {},
    }
    if kind == "calendar":                              # per-tenant tz (dispatcher decides due-ness)
        disp["cron"] = cron
        if grace is not None:
            disp["grace"] = grace                       # else dispatcher uses TENANT_BEAT default
        # fanout_period is baked into the beat cadence at load time; its default is the module
        # constant FANOUT_PERIOD_DEFAULT (NOT settings.TENANT_BEAT / beat_conf — reading
        # django.conf.settings during settings load would cache an incomplete settings object).
        beat_schedule = float(fanout_period if fanout_period is not None
                              else FANOUT_PERIOD_DEFAULT)
    else:                                               # interval -> fan out to all at this cadence
        beat_schedule = sched

    out = SchedEntry({"task": FANOUT_TASK_NAME,
                      "schedule": beat_schedule, "kwargs": disp})
    out.scope = scope
    return out


# ---------------------------------------------------------------------------
# Task-level queue helper (mode-aware). Not scheduling per se, but the other Celery
# application-facing shim — kept here so app code has ONE Celery-helpers import surface.
# ---------------------------------------------------------------------------
def task_queue(name):
    """Queue for a task: `name` under multitenant, else None so Celery routes to the
    effective task_default_queue (the host's CELERY_TASK_DEFAULT_QUEUE or Celery's built-in
    default) — the queue the host's existing workers already consume. Keeps routing at the
    task level (@shared_task(queue=task_queue("..."))) while letting a standalone drop-in
    change nothing about task routing."""
    return name if settings.USE_MULTITENANT else None
