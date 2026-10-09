"""Fanout dispatch — Phase A (interval + wiring). DB-free (SimpleTestCase); the
dispatcher's DB/cache/broker touch-points are mocked. Full design:
deploy/celery_fanout_design.md."""
from typing import Any
from datetime import datetime, timedelta, timezone as dt_tz
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from celery.schedules import crontab, schedule as interval_schedule
from kombu.exceptions import OperationalError

from ._support import FakeLock, FakeLockRedis
from commons.platform.beat import _classify_schedule, _crontab_to_cronspec, scoped_schedule, scope_of
from commons.platform.beat import task_queue
from tenants import checks
from tenants.celery import dispatch


def _utc(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=dt_tz.utc)


class ClassifyScheduleTests(SimpleTestCase):
    def test_interval_kinds(self) -> None:
        for s in (5.0, 45, timedelta(seconds=30), interval_schedule(run_every=10)):
            kind, sched, cron = _classify_schedule(s)
            self.assertEqual(kind, "interval")
            self.assertIsNone(cron)

    def test_crontab_is_calendar(self) -> None:
        kind, sched, cron = _classify_schedule(crontab(minute=0, hour=8))
        self.assertEqual(kind, "calendar")
        self.assertEqual(cron, "0 8 * * *")

    def test_crontab_cronspec_preserves_fields(self) -> None:
        self.assertEqual(_crontab_to_cronspec(crontab(minute="25,45")), "25,45 * * * *")

    def test_string_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _classify_schedule("*/5 * * * *")

    def test_unknown_type_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _classify_schedule(object())


class SchedWrapperTests(SimpleTestCase):
    def test_standalone_is_identity(self) -> None:
        # scoped_schedule resolves the mode via commons.platform.mode.use_multitenant (settings-load-safe),
        # NOT django.conf.settings — so patch that, not override_settings.
        entry = {"task": "t", "schedule": 5.0, "args": (1,)}
        with mock.patch("commons.platform.beat.use_multitenant", return_value=False):
            self.assertEqual(scoped_schedule(entry, scope="tenants"), entry)

    def test_public_is_passthrough(self) -> None:
        entry = {"task": "t", "schedule": crontab(minute=0, hour=3)}
        self.assertEqual(scoped_schedule(entry, scope="public"), entry)

    def test_mt_interval_wraps_to_dispatcher_same_cadence(self) -> None:
        out = scoped_schedule({"task": "app.t", "schedule": 5.0, "args": (7,),
                      "kwargs": {"k": 1}}, scope="tenants")
        self.assertEqual(out["task"], "tenants.tasks.fanout_dispatch")
        self.assertEqual(out["schedule"], 5.0)                 # interval keeps its cadence
        self.assertEqual(out["kwargs"]["task_name"], "app.t")
        self.assertEqual(out["kwargs"]["scope"], "tenants")
        self.assertEqual(out["kwargs"]["task_args"], [7])
        self.assertEqual(out["kwargs"]["task_kwargs"], {"k": 1})
        self.assertNotIn("cron", out["kwargs"])                # interval has no cron

    def test_mt_calendar_wraps_with_cron_and_fanout_period(self) -> None:
        out = scoped_schedule({"task": "app.daily", "schedule": crontab(minute=0, hour=8)},
                     scope="tenants")
        self.assertEqual(out["task"], "tenants.tasks.fanout_dispatch")
        self.assertEqual(out["schedule"], 60.0)                # beat tick = fanout_period
        self.assertEqual(out["kwargs"]["cron"], "0 8 * * *")

    def test_mt_calendar_per_task_grace_and_period(self) -> None:
        out = scoped_schedule({"task": "app.daily", "schedule": crontab(minute=0, hour=8)},
                     scope="tenants", fanout_period=30, grace=120)
        self.assertEqual(out["schedule"], 30.0)
        self.assertEqual(out["kwargs"]["grace"], 120)

    def test_invalid_scope_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            scoped_schedule({"task": "t", "schedule": 5.0}, scope="both")


class TaskQueueTests(SimpleTestCase):
    def test_multitenant_returns_name(self) -> None:
        self.assertEqual(task_queue("fanout"), "fanout")

    @override_settings(USE_MULTITENANT=False)
    def test_standalone_returns_none(self) -> None:
        self.assertIsNone(task_queue("fanout"))


class FanoutDispatchTests(SimpleTestCase):
    def test_argsig_distinguishes_args(self) -> None:
        self.assertNotEqual(dispatch.argsig([1]), dispatch.argsig([7]))

    def test_argsig_is_empty_for_no_args(self) -> None:
        """Most entries take no args; "" keeps their lock key and their TaskRun rows
        readable instead of stamping a constant digest on all of them. It can never collide
        with a real signature, which is 12 hex chars."""
        self.assertEqual(dispatch.argsig(None), "")
        self.assertEqual(dispatch.argsig([]), "")
        self.assertEqual(len(dispatch.argsig([7])), 12)
        self.assertNotEqual(dispatch.argsig([7]), "")

    def test_overlap_lock_skips(self) -> None:
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=FakeLock(acquired=False)), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            out = dispatch.fanout_dispatch.run("app.t", scope="tenants")
        self.assertEqual(out, {"skipped": "overlapping"})
        delay.assert_not_called()

    def test_interval_fans_out_all_in_one_batch(self) -> None:
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=FakeLock()), \
             mock.patch("tenants.celery.dispatch.active_target_schemas",
                        return_value=["a", "b", "c"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            out = dispatch.fanout_dispatch.run("app.t", scope="tenants")
        self.assertEqual(out["fanned_out"], 3)
        delay.assert_called_once()
        self.assertEqual(delay.call_args.args[1], ["a", "b", "c"])  # schemas batch

    def test_interval_batches(self) -> None:
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=FakeLock()), \
             mock.patch("tenants.celery.dispatch.active_target_schemas",
                        return_value=["a", "b", "c", "d", "e"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            dispatch.fanout_dispatch.run("app.t", scope="tenants", batch_size=2)
        self.assertEqual(delay.call_count, 3)                  # 2 + 2 + 1

    def test_calendar_fans_out_due_subset_with_run_ts(self) -> None:
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=FakeLock()), \
             mock.patch("tenants.celery.dispatch._due_by_tenant_tz",
                        return_value=["a", "b"]) as due, \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            out = dispatch.fanout_dispatch.run("app.daily", cron="0 8 * * *")
        self.assertEqual(out["fanned_out"], 2)
        # grace default (from TENANT_BEAT) passed to the tz due-check
        self.assertEqual(due.call_args.args[0], "app.daily")
        self.assertEqual(due.call_args.args[4], 300)          # grace default
        # sub_dispatch got a non-null run_ts (watermark) for the calendar wave
        self.assertIsNotNone(delay.call_args.args[5])


class SameTaskDifferentArgsTests(SimpleTestCase):
    """Two CALENDAR entries sharing a task NAME but differing by args are INDEPENDENT
    schedules — `fetch(1)` and `fetch(7)` each keep their own overlap-lock and their own
    per-tenant watermark.

    Keyed by task name alone, the wave that lands second reads the first one's watermark,
    finds the occurrence already run, and is skipped — indefinitely, and silently, since the
    two never contend for the lock. That is why TaskRun's key includes args_sig
    (migration 0008) and why one signature is computed per wave and threaded through.
    """

    def test_lock_and_watermark_use_the_same_signature(self) -> None:
        """The two must never disagree about what 'the same schedule entry' is."""
        seen = {}

        def _record_lock(task_name, sig):
            seen["lock"] = sig
            return FakeLock()

        with mock.patch("tenants.celery.dispatch._wave_lock", side_effect=_record_lock), \
             mock.patch("tenants.celery.dispatch._due_by_tenant_tz",
                        side_effect=lambda n, sig, *a: seen.setdefault("due", sig) or ["a"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            dispatch.fanout_dispatch.run("app.fetch", cron="0 8 * * *", task_args=[7])
        self.assertEqual(seen["lock"], dispatch.argsig([7]))
        self.assertEqual(seen["due"], seen["lock"])
        self.assertEqual(delay.call_args.args[6], seen["lock"])   # threaded to sub_dispatch

    def test_two_arg_variants_read_separate_watermarks(self) -> None:
        """The bug this guards: fetch(1) marking its watermark must not make fetch(7)
        look already-run for the same occurrence."""
        calls = []
        with mock.patch("tenants.celery.dispatch.active_tenants_with_tz",
                        return_value=[("alpha", "UTC")]), \
             mock.patch.object(dispatch.TaskRun, "load_map",
                               side_effect=lambda t, sig: calls.append((t, sig)) or {}):
            now = datetime(2026, 6, 15, 8, 0, 30, tzinfo=dt_tz.utc)
            for args in ([1], [7]):
                dispatch._due_by_tenant_tz("app.fetch", dispatch.argsig(args),
                                           "0 8 * * *", now, 300)
        self.assertEqual([t for t, _ in calls], ["app.fetch", "app.fetch"])
        self.assertNotEqual(calls[0][1], calls[1][1])          # distinct watermark keys

    def test_sub_dispatch_stamps_the_watermark_it_was_given(self) -> None:
        with mock.patch("tenants.celery.dispatch.current_app"), \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            dispatch.sub_dispatch.run("app.fetch", ["alpha"], task_args=[7],
                                      run_ts="2026-06-15T08:00:30+00:00",
                                      args_sig="deadbeefcafe")
        self.assertEqual(mark.call_args.args[1], "deadbeefcafe")

    def test_sub_dispatch_recomputes_the_signature_when_absent(self) -> None:
        """A message enqueued by a previous release carries no args_sig; recomputing keeps
        it working instead of a TypeError under acks_late + max_retries=0."""
        with mock.patch("tenants.celery.dispatch.current_app"), \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            dispatch.sub_dispatch.run("app.fetch", ["alpha"], task_args=[7],
                                      run_ts="2026-06-15T08:00:30+00:00")
        self.assertEqual(mark.call_args.args[1], dispatch.argsig([7]))


class SubDispatchTests(SimpleTestCase):
    def test_sends_per_schema_with_schema_header(self) -> None:
        with mock.patch("tenants.celery.dispatch.current_app") as app:
            out = dispatch.sub_dispatch.run("app.t", ["alpha", "beta"],
                                         task_args=[1], task_kwargs={"x": 2})
        self.assertEqual(out["sent"], 2)
        self.assertEqual(app.send_task.call_count, 2)
        headers = [c.kwargs["headers"]["_schema_name"] for c in app.send_task.call_args_list]
        self.assertEqual(headers, ["alpha", "beta"])
        first = app.send_task.call_args_list[0]
        self.assertEqual(first.kwargs["args"], [1])
        self.assertEqual(first.kwargs["kwargs"], {"x": 2})

    def test_send_failure_is_counted_not_marked(self) -> None:
        """OperationalError, not a bare Exception: only a TRANSIENT broker failure is counted
        and retried next tick. A deterministic one now propagates — see
        SubDispatchOptionsTests.test_a_deterministic_failure_is_raised_not_swallowed."""
        with mock.patch("tenants.celery.dispatch.current_app") as app, \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            app.send_task.side_effect = [None, OperationalError("broker hiccup")]
            out = dispatch.sub_dispatch.run("app.t", ["alpha", "beta"])
        self.assertEqual(out, {"task": "app.t", "sent": 1, "requested": 2})
        mark.assert_not_called()                       # interval (run_ts None) never marks

    def test_calendar_marks_taskrun_for_sent_only(self) -> None:
        with mock.patch("tenants.celery.dispatch.current_app") as app, \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            app.send_task.side_effect = [None, OperationalError("hiccup")]
            dispatch.sub_dispatch.run("app.daily", ["alpha", "beta"], run_ts="2026-06-15T08:00:30+00:00")
        # keyed by args_sig as well as task — see TaskRun / migration 0008
        mark.assert_called_once_with("app.daily", dispatch.argsig(None), ["alpha"],
                                     "2026-06-15T08:00:30+00:00")


class DueByTenantTzTests(SimpleTestCase):
    """Pure tz/grace logic — active_tenants_with_tz + TaskRun.load_map mocked (DB-free)."""

    def _due(self, tenants: Any, now: datetime, grace: float, last: Any = None) -> list[str]:
        with mock.patch("tenants.celery.dispatch.active_tenants_with_tz", return_value=tenants), \
             mock.patch.object(dispatch.TaskRun, "load_map", return_value=last or {}):
            # 2nd arg is the args SIGNATURE, not the raw args
            return dispatch._due_by_tenant_tz("t", dispatch.argsig(None),
                                              "0 8 * * *", now, grace)

    def test_on_time_fires(self) -> None:
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 0, 30), 300), ["a"])

    def test_too_late_skips(self) -> None:
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 10, 0), 300), [])

    def test_self_heal_within_grace(self) -> None:
        last = {"a": _utc(2026, 6, 14, 8, 0, 0)}                 # yesterday
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 4, 0), 300, last), ["a"])

    def test_already_ran_skips(self) -> None:
        last = {"a": _utc(2026, 6, 15, 8, 0, 0)}                 # today's occurrence
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 0, 30), 300, last), [])

    def test_no_retroactive_fire_on_deploy(self) -> None:
        # first sight (no last_run), but the 08:00 occurrence is 60 min old > grace -> skip
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 9, 0, 0), 300), [])

    def test_per_tenant_timezone(self) -> None:
        # same UTC instant: 08:00 local for A (UTC+3) but 00:00 local for B (UTC-5)
        tenants = [("a", "Etc/GMT-3"), ("b", "Etc/GMT+5")]      # UTC+3 / UTC-5
        self.assertEqual(self._due(tenants, _utc(2026, 6, 15, 5, 0, 30), 300), ["a"])


class DroppedOccurrenceWarningTests(SimpleTestCase):
    """The grace skip is DESIGNED (§3.1) but used to be invisible: the only trace was a
    TaskRun row that stopped advancing. These pin the warning AND both guards that keep it
    from becoming a per-tick, per-tenant flood — a log nobody can read is the same as none."""

    LOG = "tenants.celery.dispatch"

    def _due(self, tenants: Any, now: datetime, grace: float, last: Any = None) -> list[str]:
        with mock.patch("tenants.celery.dispatch.active_tenants_with_tz", return_value=tenants), \
             mock.patch.object(dispatch.TaskRun, "load_map", return_value=last or {}):
            return dispatch._due_by_tenant_tz("t", dispatch.argsig(None),
                                              "0 8 * * *", now, grace)

    def test_warns_when_a_known_tenant_passes_grace_undispatched(self) -> None:
        last = {"a": _utc(2026, 6, 14, 8, 0, 0)}                 # ran yesterday, not today
        with self.assertLogs(self.LOG, level="WARNING") as logs:
            due = self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 6, 0), 300, last)
        self.assertEqual(due, [])
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIn("occurrence DROPPED", logs.output[0])
        self.assertIn("1 tenant(s)", logs.output[0])

    def test_silent_for_a_tenant_that_never_ran_this_entry(self) -> None:
        """A newly added schedule entry would otherwise report a miss for EVERY tenant on
        EVERY tick until its first occurrence — the same case test_no_retroactive_fire_on_deploy
        pins for the dispatch decision."""
        with mock.patch.object(dispatch.logger, "warning") as warn:
            self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 9, 0, 0), 300), [])
        warn.assert_not_called()

    def test_silent_once_past_the_band(self) -> None:
        """t_fire does not move until the next occurrence, so without an upper bound one
        missed daily task logs on every tick for the rest of the day."""
        last = {"a": _utc(2026, 6, 14, 8, 0, 0)}
        with mock.patch.object(dispatch.logger, "warning") as warn:
            # 20 min past the 08:00 occurrence: > 2 * grace (600s)
            self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 20, 0), 300, last), [])
        warn.assert_not_called()

    def test_silent_while_still_within_grace(self) -> None:
        """Inside grace the occurrence is still DUE, not dropped — warning here would fire on
        the very tick that dispatches it."""
        last = {"a": _utc(2026, 6, 14, 8, 0, 0)}
        with mock.patch.object(dispatch.logger, "warning") as warn:
            self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 4, 0), 300, last), ["a"])
        warn.assert_not_called()

    def test_one_line_for_the_whole_wave_not_one_per_tenant(self) -> None:
        last = {c: _utc(2026, 6, 14, 8, 0, 0) for c in "abcdefg"}
        with self.assertLogs(self.LOG, level="WARNING") as logs:
            self._due([(c, "UTC") for c in "abcdefg"], _utc(2026, 6, 15, 8, 6, 0), 300, last)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("7 tenant(s)", logs.output[0])
        self.assertIn("a, b, c, d, e", logs.output[0])           # sample capped at 5
        self.assertNotIn(" f,", logs.output[0])


@override_settings(USE_MULTITENANT=True)
class BeatGraceCheckTests(SimpleTestCase):
    def _entry(self, grace: float | None = None, period: float = 60.0) -> dict[str, Any]:
        kw = {"task_name": "app.daily", "scope": "tenants", "cron": "0 8 * * *"}
        if grace is not None:
            kw["grace"] = grace
        return {"task": "tenants.tasks.fanout_dispatch", "schedule": period, "kwargs": kw}

    def test_ok_when_grace_ge_period(self) -> None:
        with override_settings(CELERY_BEAT_SCHEDULE={"d": self._entry(grace=300, period=60.0)},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 300}):
            self.assertEqual(checks.beat_grace_ge_fanout_period(None), [])

    def test_error_when_grace_lt_period(self) -> None:
        with override_settings(CELERY_BEAT_SCHEDULE={"d": self._entry(grace=30, period=60.0)},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 300}):
            errs = checks.beat_grace_ge_fanout_period(None)
        self.assertEqual([e.id for e in errs], ["tenants.E002"])

    def test_default_grace_used_when_absent(self) -> None:
        with override_settings(CELERY_BEAT_SCHEDULE={"d": self._entry(grace=None, period=60.0)},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 30}):     # default 30 < 60
            errs = checks.beat_grace_ge_fanout_period(None)
        self.assertEqual([e.id for e in errs], ["tenants.E002"])

    def test_interval_entry_ignored(self) -> None:
        entry = {"task": "tenants.tasks.fanout_dispatch", "schedule": 5.0,
                 "kwargs": {"task_name": "app.t", "scope": "tenants"}}   # no cron
        with override_settings(CELERY_BEAT_SCHEDULE={"i": entry},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 300}):
            self.assertEqual(checks.beat_grace_ge_fanout_period(None), [])


class ScopedScheduleScopeTests(SimpleTestCase):
    def test_scope_recorded_for_public_and_tenants(self) -> None:
        pub = scoped_schedule({"task": "t", "schedule": crontab(hour=3)}, scope="public")
        ten = scoped_schedule({"task": "t", "schedule": 5.0}, scope="tenants")
        self.assertEqual(scope_of(pub), "public")
        self.assertEqual(scope_of(ten), "tenants")

    def test_scope_none_for_raw_entry(self) -> None:
        self.assertIsNone(scope_of({"task": "t", "schedule": 5.0}))

    def test_scope_invisible_to_celery(self) -> None:
        """The SchedEntry contract: scope rides as an ATTRIBUTE, not a dict item. Celery reads
        the entry via `**entry`, so scope must NOT appear as a key (an unknown key would raise),
        yet scope_of() still sees it. Guards against a future `{**entry}` / dict() refactor that
        would silently drop the scope and break the tenants.E003 check."""
        e = scoped_schedule({"task": "t", "schedule": 5.0}, scope="tenants")
        self.assertNotIn("scope", dict(e))            # invisible to Celery's **entry unpacking
        self.assertEqual(scope_of(e), "tenants")      # but our getattr-based reader sees it

        from celery.beat import ScheduleEntry
        ScheduleEntry(**e, name="t", app=None)        # must NOT raise (no unknown 'scope' kwarg)

        # Documented footgun, locked in: a spread/cast DROPS the attribute (plain dict) — if this
        # ever starts passing with a non-None scope, the SchedEntry contract changed, revisit.
        self.assertIsNone(scope_of({**e}))


@override_settings(USE_MULTITENANT=True)
class BeatEntriesWrappedCheckTests(SimpleTestCase):
    def test_all_wrapped_ok(self) -> None:
        sched = {
            "a": scoped_schedule({"task": "t", "schedule": 5.0}, scope="tenants"),
            "b": scoped_schedule({"task": "t2", "schedule": crontab(hour=3)}, scope="public"),
        }
        with override_settings(CELERY_BEAT_SCHEDULE=sched):
            self.assertEqual(checks.beat_entries_wrapped(None), [])

    def test_raw_entry_flagged(self) -> None:
        sched = {
            "wrapped": scoped_schedule({"task": "t", "schedule": 5.0}, scope="tenants"),
            "raw": {"task": "t2", "schedule": 5.0},   # bypassed scoped_schedule
        }
        with override_settings(CELERY_BEAT_SCHEDULE=sched):
            errs = checks.beat_entries_wrapped(None)
        self.assertEqual([e.id for e in errs], ["tenants.E003"])


@override_settings(USE_MULTITENANT=True)
class FanoutTaskRegisteredCheckTests(SimpleTestCase):
    """tenants.E005 — the emitted FANOUT_TASK_NAME must resolve to a registered Celery task."""

    def test_ok_when_registered(self) -> None:
        from tenants import checks
        self.assertEqual(checks.fanout_task_registered(None), [])

    def test_fires_on_name_drift(self) -> None:
        from unittest import mock
        from tenants import checks
        # emit a name nothing registers -> the check must flag it (would be a worker NotRegistered).
        # Patch where the check READS the constant (its own module namespace), not the source.
        with mock.patch("tenants.checks.beat.FANOUT_TASK_NAME", "tenants.tasks.NONEXISTENT"):
            errs = checks.fanout_task_registered(None)
        self.assertTrue(any(e.id == "tenants.E005" for e in errs))


def _cal(args: Any, *, grace: float | None = None,
             fanout_period: float | None = None) -> Any:
    """A wrapped CALENDAR fanout entry (08:00 daily) with the given args."""
    kw = {}
    if grace is not None:
        kw["grace"] = grace
    if fanout_period is not None:
        kw["fanout_period"] = fanout_period
    return scoped_schedule({"task": "x.report", "schedule": crontab(minute=0, hour=8),
                            "args": args}, scope="tenants", **kw)


class FanoutEntryUniquenessTests(SimpleTestCase):
    """tenants.E006 — (task_name, args) IS the overlap-lock key, so two entries sharing it
    suppress each other. Identical entries look fine (one INFO line per tick); entries that
    share the pair but differ in cron/interval/grace are nondeterministic."""

    def _errs(self, schedule: dict[str, Any]) -> list[Any]:
        with override_settings(CELERY_BEAT_SCHEDULE=schedule):
            return checks.fanout_entries_are_unique(None)

    def test_distinct_args_are_fine(self) -> None:
        """The legitimate shape: fetch(1) and fetch(7) get distinct lock keys AND distinct
        TaskRun watermarks."""
        self.assertEqual(self._errs({"a": _cal([1]), "b": _cal([7])}), [])

    def test_single_entry_is_fine(self) -> None:
        self.assertEqual(self._errs({"a": _cal([7])}), [])

    def test_public_scope_entries_are_ignored(self) -> None:
        """scope='public' is a passthrough — not a fanout entry, so not this check's business."""
        pub = scoped_schedule({"task": "x.cleanup", "schedule": crontab(minute=0, hour=3)},
                              scope="public")
        self.assertEqual(self._errs({"a": pub, "b": pub}), [])

    def test_identical_entries_are_flagged(self) -> None:
        errs = self._errs({"report_b": _cal([7]), "report_c": _cal([7])})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])
        self.assertIn("['report_b', 'report_c']", errs[0].msg)
        self.assertIn("identical", errs[0].msg)
        self.assertIn("Delete the duplicate", errs[0].hint)

    def test_same_args_different_grace_reports_the_nondeterminism(self) -> None:
        """Worse than a duplicate: the applied grace depends on which wave takes the lock."""
        errs = self._errs({"b": _cal([7], grace=300), "c": _cal([7], grace=60)})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])
        self.assertIn("nondeterministic", errs[0].msg)

    def test_same_args_different_fanout_period_is_flagged(self) -> None:
        errs = self._errs({"b": _cal([7], fanout_period=60),
                           "c": _cal([7], fanout_period=30)})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])

    def test_calendar_and_interval_for_the_same_args_collide(self) -> None:
        """The lock key carries neither cron nor scope, so a calendar entry and an interval
        entry for the same task+args fight over one key."""
        interval = scoped_schedule({"task": "x.report", "schedule": 300.0, "args": [7]},
                                   scope="tenants")
        errs = self._errs({"cal": _cal([7]), "iv": interval})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])

    def test_one_error_per_colliding_group(self) -> None:
        errs = self._errs({"b": _cal([7]), "c": _cal([7]),
                           "d": _cal([1]), "e": _cal([1]),
                           "f": _cal([9])})
        self.assertEqual(len(errs), 2)                      # two groups, one lone entry


class WaveLockTests(SimpleTestCase):
    """The lock BODY — never exercised before, which is why the missing release survived.

    _acquire_lock used to be mocked at all five of its call sites, so `cache.add` with a TTL
    and no release never ran in the suite. The consequence was invisible: an overlap lock that
    is never released is a rate limiter at LOCK_SECONDS, and every interval entry shorter than
    60s silently fanned out once per minute.
    """

    def _lock_for(self, fake):
        with mock.patch("tenants.celery.dispatch.django_redis_raw_client", return_value=fake):
            return dispatch._wave_lock("app.push", "abc123")

    def test_locks_the_signature_keyed_name_on_the_beat_lock_alias(self) -> None:
        """Key = beat:fanout:<task>:<argsig> — no cron, no scope. tenants.E006 relies on that
        exact shape to reject two entries that would suppress each other."""
        fake = FakeLockRedis(FakeLock())
        with mock.patch("tenants.celery.dispatch.django_redis_raw_client", return_value=fake) as client:
            dispatch._wave_lock("app.push", "abc123")
        client.assert_called_once_with("beat_lock")
        name, timeout = fake.lock_calls[0]
        self.assertEqual(name, "beat:fanout:app.push:abc123")
        self.assertEqual(timeout, 60)                      # LOCK_SECONDS default

    @override_settings(TENANT_BEAT={"LOCK_SECONDS": 7})
    def test_ttl_follows_the_setting(self) -> None:
        fake = FakeLockRedis(FakeLock())
        self._lock_for(fake)
        self.assertEqual(fake.lock_calls[0][1], 7)

    def test_a_finished_wave_releases_so_the_next_tick_is_not_skipped(self) -> None:
        """The whole point of the fix: release on the way out, so a 50ms wave does not hold
        the key for the full TTL and starve a 5s schedule down to one run a minute."""
        lock = FakeLock(acquired=True)
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=lock), \
             mock.patch("tenants.celery.dispatch.active_target_schemas", return_value=["a"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay"):
            out = dispatch.fanout_dispatch.run("app.push")
        self.assertEqual(out["fanned_out"], 1)
        self.assertEqual(lock.release_calls, 1)

    def test_a_wave_that_raises_still_releases(self) -> None:
        lock = FakeLock(acquired=True)
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=lock), \
             mock.patch("tenants.celery.dispatch.active_target_schemas",
                        side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                dispatch.fanout_dispatch.run("app.push")
        self.assertEqual(lock.release_calls, 1)

    def test_a_held_lock_skips_without_releasing_it(self) -> None:
        """Never release a lock we did not take — that is someone else's wave."""
        lock = FakeLock(acquired=False)
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=lock), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            out = dispatch.fanout_dispatch.run("app.push")
        self.assertEqual(out, {"skipped": "overlapping"})
        delay.assert_not_called()
        self.assertEqual(lock.release_calls, 0)

    def test_an_expired_lock_logs_a_warning_and_does_not_propagate(self) -> None:
        """A wave slower than LOCK_SECONDS has already lost the key to a later wave. Releasing
        anyway would delete THAT wave's lock and let a third start, so redis-py refuses and we
        log instead of failing the task.

        The LEVEL and the message are asserted, not just the return value: LockError is a
        subclass of RedisError, so swapping the two arms would make the RedisError one catch
        both and report a merely-slow wave as a broker outage — pointing on-call at the wrong
        subsystem while every test still passed on the return value alone."""
        lock = FakeLock(acquired=True, release_raises=True)
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=lock), \
             mock.patch("tenants.celery.dispatch.active_target_schemas", return_value=["a"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay"):
            with self.assertLogs("tenants.celery.dispatch", level="WARNING") as logs:
                out = dispatch.fanout_dispatch.run("app.push")      # must NOT raise
        self.assertEqual(out["fanned_out"], 1)
        self.assertEqual(logs.records[0].levelname, "WARNING")
        self.assertIn("expired before release", logs.output[0])

    def test_a_redis_outage_during_release_does_not_lose_the_result(self) -> None:
        """release() runs a Lua script, so it is a network call. By then the wave has already
        dispatched — letting ConnectionError out would turn a successful fanout into a failed
        task and point the on-call at the wrong subsystem."""
        from redis.exceptions import ConnectionError as RedisConnectionError
        lock = FakeLock(acquired=True, release_error=RedisConnectionError("gone"))
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=lock), \
             mock.patch("tenants.celery.dispatch.active_target_schemas", return_value=["a"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay"):
            with self.assertLogs("tenants.celery.dispatch", level="ERROR") as logs:
                out = dispatch.fanout_dispatch.run("app.push")  # must NOT raise
        self.assertEqual(out["fanned_out"], 1)
        self.assertEqual(logs.records[0].levelname, "ERROR")
        self.assertIn("redis unreachable", logs.output[0])

    def test_a_bug_in_our_own_code_still_crashes(self) -> None:
        """The two arms are narrow on purpose: a non-Redis exception from release() is a defect
        here, not an outage, and swallowing it would hide it forever."""
        lock = FakeLock(acquired=True, release_error=TypeError("bad call"))
        with mock.patch("tenants.celery.dispatch._wave_lock", return_value=lock), \
             mock.patch("tenants.celery.dispatch.active_target_schemas", return_value=["a"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay"):
            with self.assertRaises(TypeError):
                dispatch.fanout_dispatch.run("app.push")


class SubDispatchOptionsTests(SimpleTestCase):
    """`options` is forwarded from the beat entry, so it may legally carry keys of its own.

    `headers` used to collide with the schema stamp and raise TypeError — deterministically, on
    every schema and every tick — while the loop logged it as transient and fanout_dispatch
    reported a clean wave. For calendar entries the watermark never advanced, so the occurrence
    retried until grace expired and then disappeared with no record.
    """

    def _send(self, options, schemas=("acme", "beta"), run_ts=None):
        with mock.patch.object(dispatch.current_app, "send_task") as send, \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            out = dispatch.sub_dispatch.run("app.push", list(schemas), None, None,
                                            options, run_ts, "sig")
        return out, send, mark

    def test_caller_headers_are_merged_not_collided(self) -> None:
        out, send, _ = self._send({"queue": "slow", "headers": {"x-trace": "1"}})
        self.assertEqual(out["sent"], 2)
        for call, schema in zip(send.call_args_list, ("acme", "beta")):
            self.assertEqual(call.kwargs["headers"], {"x-trace": "1", "_schema_name": schema})
            self.assertEqual(call.kwargs["queue"], "slow")       # other options still forwarded

    def test_our_stamp_wins_over_a_caller_supplied_one(self) -> None:
        """A schedule entry must not be able to choose which tenant its task runs in."""
        out, send, _ = self._send({"headers": {"_schema_name": "attacker"}}, schemas=("acme",))
        self.assertEqual(out["sent"], 1)
        self.assertEqual(send.call_args.kwargs["headers"]["_schema_name"], "acme")

    def test_options_without_headers_still_work(self) -> None:
        _, send, _ = self._send({"queue": "slow", "priority": 3}, schemas=("acme",))
        self.assertEqual(send.call_args.kwargs["headers"], {"_schema_name": "acme"})
        self.assertEqual(send.call_args.kwargs["priority"], 3)

    def test_a_broker_outage_is_swallowed_and_retried_next_tick(self) -> None:
        from kombu.exceptions import OperationalError
        with mock.patch.object(dispatch.current_app, "send_task",
                               side_effect=OperationalError("broker gone")), \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            out = dispatch.sub_dispatch.run("app.push", ["acme"], None, None, None,
                                            "2026-01-01T00:00:00", "sig")
        self.assertEqual(out, {"task": "app.push", "sent": 0, "requested": 1})
        mark.assert_not_called()                      # watermark must not advance on a miss

    def test_a_deterministic_failure_is_raised_not_swallowed(self) -> None:
        """Re-raising is the point: a bad options key would otherwise report a clean wave that
        delivered nothing, forever."""
        with mock.patch.object(dispatch.current_app, "send_task",
                               side_effect=TypeError("multiple values for 'args'")), \
             mock.patch.object(dispatch.TaskRun, "mark_ran"):
            with self.assertRaises(TypeError):
                dispatch.sub_dispatch.run("app.push", ["acme"], None, None, None, None, "sig")


class OptionsCollisionCheckTests(SimpleTestCase):
    """tenants.E009 — say it at deploy time, not on the first tick."""

    def _run(self, options):
        entry = scoped_schedule({"task": "app.push", "schedule": 30.0, "options": options},
                                scope="tenants")
        with override_settings(CELERY_BEAT_SCHEDULE={"e": entry}):
            from tenants.checks.beat import fanout_options_do_not_collide
            return fanout_options_do_not_collide(None)

    def test_delivery_options_pass(self) -> None:
        self.assertEqual(self._run({"queue": "slow", "priority": 3, "expires": 60}), [])

    def test_headers_pass_because_sub_dispatch_merges_them(self) -> None:
        self.assertEqual(self._run({"headers": {"x-trace": "1"}}), [])

    def test_args_collides(self) -> None:
        errors = self._run({"args": [1]})
        self.assertEqual([e.id for e in errors], ["tenants.E009"])
        self.assertIn("'args'", errors[0].msg)

    def test_kwargs_collides(self) -> None:
        self.assertEqual([e.id for e in self._run({"kwargs": {"a": 1}})], ["tenants.E009"])

    def test_public_scope_is_not_checked(self) -> None:
        """A public entry goes to stock beat untouched — no sub_dispatch, no collision."""
        entry = scoped_schedule({"task": "app.push", "schedule": 30.0, "options": {"args": [1]}},
                                scope="public")
        with override_settings(CELERY_BEAT_SCHEDULE={"e": entry}):
            from tenants.checks.beat import fanout_options_do_not_collide
            self.assertEqual(fanout_options_do_not_collide(None), [])
