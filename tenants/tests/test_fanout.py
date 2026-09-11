"""Fanout dispatch — Phase A (interval + wiring). DB-free (SimpleTestCase); the
dispatcher's DB/cache/broker touch-points are mocked. Full design:
deploy/celery_fanout_design.md."""
from datetime import datetime, timedelta, timezone as dt_tz
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from celery.schedules import crontab, schedule as interval_schedule

from commons.platform.beat import _classify_schedule, _crontab_to_cronspec, scoped_schedule, scope_of
from commons.platform.beat import task_queue
from tenants import checks
from tenants.celery import dispatch


def _utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=dt_tz.utc)


class ClassifyScheduleTests(SimpleTestCase):
    def test_interval_kinds(self):
        for s in (5.0, 45, timedelta(seconds=30), interval_schedule(run_every=10)):
            kind, sched, cron = _classify_schedule(s)
            self.assertEqual(kind, "interval")
            self.assertIsNone(cron)

    def test_crontab_is_calendar(self):
        kind, sched, cron = _classify_schedule(crontab(minute=0, hour=8))
        self.assertEqual(kind, "calendar")
        self.assertEqual(cron, "0 8 * * *")

    def test_crontab_cronspec_preserves_fields(self):
        self.assertEqual(_crontab_to_cronspec(crontab(minute="25,45")), "25,45 * * * *")

    def test_string_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            _classify_schedule("*/5 * * * *")

    def test_unknown_type_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            _classify_schedule(object())


class SchedWrapperTests(SimpleTestCase):
    def test_standalone_is_identity(self):
        # scoped_schedule resolves the mode via commons.platform.mode.use_multitenant (settings-load-safe),
        # NOT django.conf.settings — so patch that, not override_settings.
        entry = {"task": "t", "schedule": 5.0, "args": (1,)}
        with mock.patch("commons.platform.beat.use_multitenant", return_value=False):
            self.assertEqual(scoped_schedule(entry, scope="tenants"), entry)

    def test_public_is_passthrough(self):
        entry = {"task": "t", "schedule": crontab(minute=0, hour=3)}
        self.assertEqual(scoped_schedule(entry, scope="public"), entry)

    def test_mt_interval_wraps_to_dispatcher_same_cadence(self):
        out = scoped_schedule({"task": "app.t", "schedule": 5.0, "args": (7,),
                      "kwargs": {"k": 1}}, scope="tenants")
        self.assertEqual(out["task"], "tenants.tasks.fanout_dispatch")
        self.assertEqual(out["schedule"], 5.0)                 # interval keeps its cadence
        self.assertEqual(out["kwargs"]["task_name"], "app.t")
        self.assertEqual(out["kwargs"]["scope"], "tenants")
        self.assertEqual(out["kwargs"]["task_args"], [7])
        self.assertEqual(out["kwargs"]["task_kwargs"], {"k": 1})
        self.assertNotIn("cron", out["kwargs"])                # interval has no cron

    def test_mt_calendar_wraps_with_cron_and_fanout_period(self):
        out = scoped_schedule({"task": "app.daily", "schedule": crontab(minute=0, hour=8)},
                     scope="tenants")
        self.assertEqual(out["task"], "tenants.tasks.fanout_dispatch")
        self.assertEqual(out["schedule"], 60.0)                # beat tick = fanout_period
        self.assertEqual(out["kwargs"]["cron"], "0 8 * * *")

    def test_mt_calendar_per_task_grace_and_period(self):
        out = scoped_schedule({"task": "app.daily", "schedule": crontab(minute=0, hour=8)},
                     scope="tenants", fanout_period=30, grace=120)
        self.assertEqual(out["schedule"], 30.0)
        self.assertEqual(out["kwargs"]["grace"], 120)

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            scoped_schedule({"task": "t", "schedule": 5.0}, scope="both")


class TaskQueueTests(SimpleTestCase):
    def test_multitenant_returns_name(self):
        self.assertEqual(task_queue("fanout"), "fanout")

    @override_settings(USE_MULTITENANT=False)
    def test_standalone_returns_none(self):
        self.assertIsNone(task_queue("fanout"))


class FanoutDispatchTests(SimpleTestCase):
    def test_argsig_distinguishes_args(self):
        self.assertNotEqual(dispatch.argsig([1]), dispatch.argsig([7]))

    def test_argsig_is_empty_for_no_args(self):
        """Most entries take no args; "" keeps their lock key and their TaskRun rows
        readable instead of stamping a constant digest on all of them. It can never collide
        with a real signature, which is 12 hex chars."""
        self.assertEqual(dispatch.argsig(None), "")
        self.assertEqual(dispatch.argsig([]), "")
        self.assertEqual(len(dispatch.argsig([7])), 12)
        self.assertNotEqual(dispatch.argsig([7]), "")

    def test_overlap_lock_skips(self):
        with mock.patch("tenants.celery.dispatch._acquire_lock", return_value=False), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            out = dispatch.fanout_dispatch.run("app.t", scope="tenants")
        self.assertEqual(out, {"skipped": "overlapping"})
        delay.assert_not_called()

    def test_interval_fans_out_all_in_one_batch(self):
        with mock.patch("tenants.celery.dispatch._acquire_lock", return_value=True), \
             mock.patch("tenants.celery.dispatch.active_target_schemas",
                        return_value=["a", "b", "c"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            out = dispatch.fanout_dispatch.run("app.t", scope="tenants")
        self.assertEqual(out["fanned_out"], 3)
        delay.assert_called_once()
        self.assertEqual(delay.call_args.args[1], ["a", "b", "c"])  # schemas batch

    def test_interval_batches(self):
        with mock.patch("tenants.celery.dispatch._acquire_lock", return_value=True), \
             mock.patch("tenants.celery.dispatch.active_target_schemas",
                        return_value=["a", "b", "c", "d", "e"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            dispatch.fanout_dispatch.run("app.t", scope="tenants", batch_size=2)
        self.assertEqual(delay.call_count, 3)                  # 2 + 2 + 1

    def test_calendar_fans_out_due_subset_with_run_ts(self):
        with mock.patch("tenants.celery.dispatch._acquire_lock", return_value=True), \
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

    def test_lock_and_watermark_use_the_same_signature(self):
        """The two must never disagree about what 'the same schedule entry' is."""
        seen = {}
        with mock.patch("tenants.celery.dispatch._acquire_lock",
                        side_effect=lambda n, sig: seen.setdefault("lock", sig) or True), \
             mock.patch("tenants.celery.dispatch._due_by_tenant_tz",
                        side_effect=lambda n, sig, *a: seen.setdefault("due", sig) or ["a"]), \
             mock.patch.object(dispatch.sub_dispatch, "delay") as delay:
            dispatch.fanout_dispatch.run("app.fetch", cron="0 8 * * *", task_args=[7])
        self.assertEqual(seen["lock"], dispatch.argsig([7]))
        self.assertEqual(seen["due"], seen["lock"])
        self.assertEqual(delay.call_args.args[6], seen["lock"])   # threaded to sub_dispatch

    def test_two_arg_variants_read_separate_watermarks(self):
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

    def test_sub_dispatch_stamps_the_watermark_it_was_given(self):
        with mock.patch("tenants.celery.dispatch.current_app"), \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            dispatch.sub_dispatch.run("app.fetch", ["alpha"], task_args=[7],
                                      run_ts="2026-06-15T08:00:30+00:00",
                                      args_sig="deadbeefcafe")
        self.assertEqual(mark.call_args.args[1], "deadbeefcafe")

    def test_sub_dispatch_recomputes_the_signature_when_absent(self):
        """A message enqueued by a previous release carries no args_sig; recomputing keeps
        it working instead of a TypeError under acks_late + max_retries=0."""
        with mock.patch("tenants.celery.dispatch.current_app"), \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            dispatch.sub_dispatch.run("app.fetch", ["alpha"], task_args=[7],
                                      run_ts="2026-06-15T08:00:30+00:00")
        self.assertEqual(mark.call_args.args[1], dispatch.argsig([7]))


class SubDispatchTests(SimpleTestCase):
    def test_sends_per_schema_with_schema_header(self):
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

    def test_send_failure_is_counted_not_marked(self):
        with mock.patch("tenants.celery.dispatch.current_app") as app, \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            app.send_task.side_effect = [None, RuntimeError("broker hiccup")]
            out = dispatch.sub_dispatch.run("app.t", ["alpha", "beta"])
        self.assertEqual(out, {"task": "app.t", "sent": 1, "requested": 2})
        mark.assert_not_called()                       # interval (run_ts None) never marks

    def test_calendar_marks_taskrun_for_sent_only(self):
        with mock.patch("tenants.celery.dispatch.current_app") as app, \
             mock.patch.object(dispatch.TaskRun, "mark_ran") as mark:
            app.send_task.side_effect = [None, RuntimeError("hiccup")]
            dispatch.sub_dispatch.run("app.daily", ["alpha", "beta"], run_ts="2026-06-15T08:00:30+00:00")
        # keyed by args_sig as well as task — see TaskRun / migration 0008
        mark.assert_called_once_with("app.daily", dispatch.argsig(None), ["alpha"],
                                     "2026-06-15T08:00:30+00:00")


class DueByTenantTzTests(SimpleTestCase):
    """Pure tz/grace logic — active_tenants_with_tz + TaskRun.load_map mocked (DB-free)."""

    def _due(self, tenants, now, grace, last=None):
        with mock.patch("tenants.celery.dispatch.active_tenants_with_tz", return_value=tenants), \
             mock.patch.object(dispatch.TaskRun, "load_map", return_value=last or {}):
            # 2nd arg is the args SIGNATURE, not the raw args
            return dispatch._due_by_tenant_tz("t", dispatch.argsig(None),
                                              "0 8 * * *", now, grace)

    def test_on_time_fires(self):
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 0, 30), 300), ["a"])

    def test_too_late_skips(self):
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 10, 0), 300), [])

    def test_self_heal_within_grace(self):
        last = {"a": _utc(2026, 6, 14, 8, 0, 0)}                 # yesterday
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 4, 0), 300, last), ["a"])

    def test_already_ran_skips(self):
        last = {"a": _utc(2026, 6, 15, 8, 0, 0)}                 # today's occurrence
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 8, 0, 30), 300, last), [])

    def test_no_retroactive_fire_on_deploy(self):
        # first sight (no last_run), but the 08:00 occurrence is 60 min old > grace -> skip
        self.assertEqual(self._due([("a", "UTC")], _utc(2026, 6, 15, 9, 0, 0), 300), [])

    def test_per_tenant_timezone(self):
        # same UTC instant: 08:00 local for A (UTC+3) but 00:00 local for B (UTC-5)
        tenants = [("a", "Etc/GMT-3"), ("b", "Etc/GMT+5")]      # UTC+3 / UTC-5
        self.assertEqual(self._due(tenants, _utc(2026, 6, 15, 5, 0, 30), 300), ["a"])


@override_settings(USE_MULTITENANT=True)
class BeatGraceCheckTests(SimpleTestCase):
    def _entry(self, grace=None, period=60.0):
        kw = {"task_name": "app.daily", "scope": "tenants", "cron": "0 8 * * *"}
        if grace is not None:
            kw["grace"] = grace
        return {"task": "tenants.tasks.fanout_dispatch", "schedule": period, "kwargs": kw}

    def test_ok_when_grace_ge_period(self):
        with override_settings(CELERY_BEAT_SCHEDULE={"d": self._entry(grace=300, period=60.0)},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 300}):
            self.assertEqual(checks.beat_grace_ge_fanout_period(None), [])

    def test_error_when_grace_lt_period(self):
        with override_settings(CELERY_BEAT_SCHEDULE={"d": self._entry(grace=30, period=60.0)},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 300}):
            errs = checks.beat_grace_ge_fanout_period(None)
        self.assertEqual([e.id for e in errs], ["tenants.E002"])

    def test_default_grace_used_when_absent(self):
        with override_settings(CELERY_BEAT_SCHEDULE={"d": self._entry(grace=None, period=60.0)},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 30}):     # default 30 < 60
            errs = checks.beat_grace_ge_fanout_period(None)
        self.assertEqual([e.id for e in errs], ["tenants.E002"])

    def test_interval_entry_ignored(self):
        entry = {"task": "tenants.tasks.fanout_dispatch", "schedule": 5.0,
                 "kwargs": {"task_name": "app.t", "scope": "tenants"}}   # no cron
        with override_settings(CELERY_BEAT_SCHEDULE={"i": entry},
                               TENANT_BEAT={"TZ_GRACE_SECONDS": 300}):
            self.assertEqual(checks.beat_grace_ge_fanout_period(None), [])


class ScopedScheduleScopeTests(SimpleTestCase):
    def test_scope_recorded_for_public_and_tenants(self):
        pub = scoped_schedule({"task": "t", "schedule": crontab(hour=3)}, scope="public")
        ten = scoped_schedule({"task": "t", "schedule": 5.0}, scope="tenants")
        self.assertEqual(scope_of(pub), "public")
        self.assertEqual(scope_of(ten), "tenants")

    def test_scope_none_for_raw_entry(self):
        self.assertIsNone(scope_of({"task": "t", "schedule": 5.0}))

    def test_scope_invisible_to_celery(self):
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
    def test_all_wrapped_ok(self):
        sched = {
            "a": scoped_schedule({"task": "t", "schedule": 5.0}, scope="tenants"),
            "b": scoped_schedule({"task": "t2", "schedule": crontab(hour=3)}, scope="public"),
        }
        with override_settings(CELERY_BEAT_SCHEDULE=sched):
            self.assertEqual(checks.beat_entries_wrapped(None), [])

    def test_raw_entry_flagged(self):
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

    def test_ok_when_registered(self):
        from tenants import checks
        self.assertEqual(checks.fanout_task_registered(None), [])

    def test_fires_on_name_drift(self):
        from unittest import mock
        from tenants import checks
        # emit a name nothing registers -> the check must flag it (would be a worker NotRegistered).
        # Patch where the check READS the constant (its own module namespace), not the source.
        with mock.patch("tenants.checks.beat.FANOUT_TASK_NAME", "tenants.tasks.NONEXISTENT"):
            errs = checks.fanout_task_registered(None)
        self.assertTrue(any(e.id == "tenants.E005" for e in errs))


def _cal(args, *, grace=None, fanout_period=None):
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

    def _errs(self, schedule):
        with override_settings(CELERY_BEAT_SCHEDULE=schedule):
            return checks.fanout_entries_are_unique(None)

    def test_distinct_args_are_fine(self):
        """The legitimate shape: fetch(1) and fetch(7) get distinct lock keys AND distinct
        TaskRun watermarks."""
        self.assertEqual(self._errs({"a": _cal([1]), "b": _cal([7])}), [])

    def test_single_entry_is_fine(self):
        self.assertEqual(self._errs({"a": _cal([7])}), [])

    def test_public_scope_entries_are_ignored(self):
        """scope='public' is a passthrough — not a fanout entry, so not this check's business."""
        pub = scoped_schedule({"task": "x.cleanup", "schedule": crontab(minute=0, hour=3)},
                              scope="public")
        self.assertEqual(self._errs({"a": pub, "b": pub}), [])

    def test_identical_entries_are_flagged(self):
        errs = self._errs({"report_b": _cal([7]), "report_c": _cal([7])})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])
        self.assertIn("['report_b', 'report_c']", errs[0].msg)
        self.assertIn("identical", errs[0].msg)
        self.assertIn("Delete the duplicate", errs[0].hint)

    def test_same_args_different_grace_reports_the_nondeterminism(self):
        """Worse than a duplicate: the applied grace depends on which wave takes the lock."""
        errs = self._errs({"b": _cal([7], grace=300), "c": _cal([7], grace=60)})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])
        self.assertIn("nondeterministic", errs[0].msg)

    def test_same_args_different_fanout_period_is_flagged(self):
        errs = self._errs({"b": _cal([7], fanout_period=60),
                           "c": _cal([7], fanout_period=30)})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])

    def test_calendar_and_interval_for_the_same_args_collide(self):
        """The lock key carries neither cron nor scope, so a calendar entry and an interval
        entry for the same task+args fight over one key."""
        interval = scoped_schedule({"task": "x.report", "schedule": 300.0, "args": [7]},
                                   scope="tenants")
        errs = self._errs({"cal": _cal([7]), "iv": interval})
        self.assertEqual([e.id for e in errs], ["tenants.E006"])

    def test_one_error_per_colliding_group(self):
        errs = self._errs({"b": _cal([7]), "c": _cal([7]),
                           "d": _cal([1]), "e": _cal([1]),
                           "f": _cal([9])})
        self.assertEqual(len(errs), 2)                      # two groups, one lone entry
