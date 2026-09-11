"""Celery beat-schedule + fanout-dispatch contract invariants.
Full design: deploy/celery_fanout_design.md."""
from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error, register

from commons.platform.beat import FANOUT_TASK_NAME, beat_conf, scope_of

from .base import mt_check


@register()
@mt_check
def beat_grace_ge_fanout_period(app_configs: list[AppConfig] | None,
                                **kwargs: Any) -> list[CheckMessage]:
    """tenants.E002 — for each CALENDAR (tz) fanout entry, `grace` must be ≥ its
    `fanout_period` (the beat tick). Otherwise a tick can land outside a fresh
    occurrence's grace window and skip it. No-op until CELERY_BEAT_SCHEDULE is
    populated (cutover); reads the WRAPPED entries (task == fanout_dispatch + cron)."""
    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}
    default_grace = beat_conf("TZ_GRACE_SECONDS")
    errors = []
    for name, entry in schedule.items():
        if entry.get("task") != FANOUT_TASK_NAME:
            continue
        kw = entry.get("kwargs") or {}
        if not kw.get("cron"):
            continue                         # interval entry — no tz/grace
        grace = kw.get("grace")
        grace = default_grace if grace is None else grace
        try:
            period = float(entry.get("schedule"))
        except (TypeError, ValueError):
            continue
        if grace < period:
            errors.append(Error(
                f"CELERY_BEAT_SCHEDULE[{name!r}]: grace ({grace}s) < fanout_period ({period}s).",
                hint="A calendar (tz) task's grace must be >= its fanout_period (the beat "
                     "tick), or a tick can land outside the occurrence's grace window and "
                     "skip it. Raise grace (per-task scoped_schedule(grace=...) or "
                     "TENANT_BEAT['TZ_GRACE_SECONDS']) or lower fanout_period.",
                id="tenants.E002",
            ))
    return errors


@register()
@mt_check
def beat_entries_wrapped(app_configs: list[AppConfig] | None,
                         **kwargs: Any) -> list[CheckMessage]:
    """tenants.E003 — every CELERY_BEAT_SCHEDULE entry must be wrapped with
    commons.platform.beat.scoped_schedule, so its SCOPE is explicit and we know where it runs:
      scoped_schedule(entry, scope="public")   -> runs ONCE (e.g. the public schema)
      scoped_schedule(entry, scope="tenants")  -> fanned out per tenant
    A raw dict that bypassed scoped_schedule would run only ONCE even under multi-tenant (a silent
    per-tenant miss), so we flag it at deploy time."""
    errors = []
    for name, entry in (getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}).items():
        if isinstance(entry, dict) and scope_of(entry) is None:
            errors.append(Error(
                f"CELERY_BEAT_SCHEDULE[{name!r}] was not wrapped with commons.platform.beat.scoped_schedule.",
                hint="Wrap it so its scope is explicit: "
                     "scoped_schedule({...}, scope='public') to run once, or scope='tenants' to fan "
                     "out per tenant (crontab => per-tenant local time; a number => interval "
                     "for all tenants). A raw dict runs only once even in multi-tenant.",
                id="tenants.E003",
            ))
    return errors


@register()
@mt_check
def fanout_task_registered(app_configs: list[AppConfig] | None,
                           **kwargs: Any) -> list[CheckMessage]:
    """tenants.E005 — the fan-out task name emitted by scoped_schedule
    (commons.platform.beat.FANOUT_TASK_NAME) must actually be REGISTERED in Celery via the
    normal autodiscover path. The PRODUCER (commons, mode-agnostic, settings-load) and the
    DEFINER (tenants.celery.dispatch, @shared_task) sit on opposite sides of the one-way
    commons<-tenants boundary, so we pin the contract at deploy time instead of letting a
    mismatch (or a lost registration import) surface only as a worker-side NotRegistered.

    We import `tenants.tasks` — exactly what Celery autodiscover loads; its bottom
    `from tenants.celery import dispatch` import registers the dispatch tasks — then confirm
    the emitted name resolves in the app's task registry. NB: this DELIBERATELY finalizes the
    Celery app (accessing current_app.tasks) as a side effect of the check."""
    import importlib
    from celery import current_app

    importlib.import_module("tenants.tasks")   # side-effect: its bottom import registers dispatch

    if FANOUT_TASK_NAME not in current_app.tasks:
        return [Error(
            f"The fan-out dispatch task {FANOUT_TASK_NAME!r} (emitted into CELERY_BEAT_SCHEDULE "
            f"by commons.platform.beat.scoped_schedule) is NOT registered in Celery.",
            hint="tenants.celery.dispatch must register it via @shared_task(name=FANOUT_TASK_NAME), "
                 "and that module must be imported by autodiscover — it is wired through the bottom "
                 "`from tenants.celery import dispatch` import in tenants/tasks.py. A missing "
                 "registration would fail at runtime as a worker-side NotRegistered.",
            id="tenants.E005",
        )]
    return []


@register()
@mt_check
def fanout_entries_are_unique(app_configs: list[AppConfig] | None,
                              **kwargs: Any) -> list[CheckMessage]:
    """tenants.E006 — no two fanout entries may share (task_name, args).

    That pair IS the overlap-lock key (dispatch._acquire_lock builds
    `beat:fanout:<task>:<argsig>` — no cron, no scope), so entries sharing it SUPPRESS each
    other: the first wave of a tick takes the lock, the rest return "skipped (overlapping)".

    For an accidental duplicate the only symptom is one permanent INFO line per tick,
    indistinguishable from a genuinely slow previous wave. For entries that share the pair
    but differ in cron / interval / grace it is worse than invisible: whichever wave wins
    the lock applies ITS grace, so the task's max lateness is nondeterministic between the
    declared values, and a shorter fanout_period is silently capped by LOCK_SECONDS.

    Matches on the SIGNATURE rather than the raw args deliberately — it is the exact value
    the lock keys on, so this check cannot disagree with runtime about what "the same
    entry" means. Imported lazily: dispatch pulls in tenants.models, and checks are
    registered from apps.ready().
    """
    from tenants.celery.dispatch import argsig

    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}
    groups = {}
    for name, entry in schedule.items():
        if entry.get("task") != FANOUT_TASK_NAME:
            continue
        kw = entry.get("kwargs") or {}
        groups.setdefault((kw.get("task_name"), argsig(kw.get("task_args"))), []).append(
            (name, entry))

    errors = []
    for (task_name, _sig), members in groups.items():
        if len(members) < 2:
            continue
        names = sorted(n for n, _ in members)
        graces = {(e.get("kwargs") or {}).get("grace") for _, e in members}
        crons = {(e.get("kwargs") or {}).get("cron") for _, e in members}
        periods = {e.get("schedule") for _, e in members}
        if len(graces) > 1 or len(crons) > 1 or len(periods) > 1:
            what = ("They differ in cron / interval / grace, so they are not even a plain "
                    "duplicate: they will suppress each other nondeterministically and the "
                    "applied grace depends on which wave wins the lock.")
            fix = ("Merge them into one entry. Distinct args would split the lock key and "
                   "the TaskRun watermark, but that is only an option if the task takes "
                   "args at all — a no-args task has nothing to differentiate on, so two "
                   "schedules for it cannot coexist.")
        else:
            what = "They are identical, so one of them can never do any work."
            fix = "Delete the duplicate."
        errors.append(Error(
            f"CELERY_BEAT_SCHEDULE entries {names} all fan out {task_name!r} with the same "
            f"args, so they share one overlap-lock key. {what}",
            hint=fix,
            id="tenants.E006",
        ))
    return errors
