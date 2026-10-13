"""DB-free tests for the migrate_schemas command (deploy/migrations_plan_rev7.md §20.2,
steps 3–4 of §21): PickNextTests, ExitCodeTests, FlagTests, SummaryJsonTests, RunLogTests
(the file-system half; the DB half is in db_migrate_schemas.py), ParallelTests,
SignalTests, LegacyCommandTests.

ParallelTests really fork: the mocked migrate_tenant is copied into the workers with the
rest of the process, so they run without a database too. Results travel back pickled;
call counts on the mock do not (each worker has its own copy).

The command runs with tenants.migrator's per-target functions and target selection mocked:
these tests pin the command's own logic, not the migrator's.
"""
import ctypes
import io
import json
import os
import re
import signal
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command, load_command_class
from django.core.management.base import BaseCommand
from django.test import SimpleTestCase, override_settings

from commons.platform.commands import TenantCommand
from tenants import migrator
from tenants.management.commands import migrate_schemas as ms
from tenants.migrator import Reason, Result

CMD = "tenants.management.commands.migrate_schemas.Command"


def _entry(schema: str, shard: str = "tenant_1", pk: int = 1, status: str = "active") -> ms.Entry:
    return ms.Entry(ms.Target("tenant", schema, shard, pk, status))


def _targets(*spec: tuple[str, str]) -> list[ms.Target]:
    return [ms.Target("tenant", s, shard, i, "active") for i, (s, shard) in enumerate(spec, 1)]


def _res(kind: str, schema: str = "x", shard: str = "tenant_1", reason: str = "",
         **kw) -> Result:
    return Result(kind, schema, shard, reason or {"ok": "applied", "noop": "up_to_date",
                                                   "failed": "migration_error",
                                                   "skipped": "locked"}[kind], **kw)


# ---------------------------------------------------------------------------
class PickNextTests(SimpleTestCase):
    def test_round_robin_over_shards_fifo_within(self) -> None:
        st = ms.TenantStage.of([_entry("a1", "s1"), _entry("a2", "s1"), _entry("b1", "s2")],
                               keep_going=True, max_failures=3)
        order = []
        while (e := st.next(1, 1)) is not None:
            order.append(e.target.schema)
            e.kind = "ok"
            st.done(e)
        self.assertEqual(order, ["a1", "b1", "a2"])

    def test_global_jobs_limit(self) -> None:
        q = {"s1": deque([1, 2]), "s2": deque([3])}
        self.assertIsNone(ms.pick_next(q, {"s1": 1, "s2": 1}, jobs=2, per_shard=2, after=None))
        self.assertEqual(ms.pick_next(q, {"s1": 1}, jobs=2, per_shard=2, after="s1"), "s2")

    def test_per_shard_limit(self) -> None:
        q = {"s1": deque([1, 2]), "s2": deque()}
        self.assertIsNone(ms.pick_next(q, {"s1": 1}, jobs=4, per_shard=1, after=None))
        self.assertEqual(ms.pick_next(q, {"s1": 1}, jobs=4, per_shard=2, after=None), "s1")

    def test_after_wraps_around(self) -> None:
        q = {"s1": deque([1]), "s2": deque([2]), "s3": deque([3])}
        self.assertEqual(ms.pick_next(q, {}, 9, 9, after="s3"), "s1")
        self.assertEqual(ms.pick_next(q, {}, 9, 9, after="s1"), "s2")
        self.assertEqual(ms.pick_next(q, {}, 9, 9, after="gone"), "s1")

    def _run(self, kinds: dict[str, list[str]], keep_going: bool, max_failures: int = 2,
             conn: dict[str, str] | None = None) -> tuple[ms.TenantStage, list[ms.Entry]]:
        """Drive a stage sequentially; kinds[shard] lists the outcome of each of its targets."""
        entries = [_entry(f"{shard}_{i}", shard) for shard, ks in kinds.items() for i in range(len(ks))]
        it = {shard: iter(ks) for shard, ks in kinds.items()}
        st = ms.TenantStage.of(entries, keep_going, max_failures)
        while (e := st.next(1, 1)) is not None:
            e.kind = next(it[e.target.shard])
            e.conn_error = (conn or {}).get(e.target.schema)
            st.done(e)
        return st, entries

    def test_fail_fast_without_keep_going(self) -> None:
        st, entries = self._run({"s1": ["failed", "ok"], "s2": ["ok"]}, keep_going=False)
        self.assertEqual(str(st.stop), "fail_fast")
        self.assertEqual([e.kind for e in entries], ["failed", "not_run", "not_run"])
        self.assertEqual(entries[1].reason, Reason.STOPPED)

    def test_last_target_failing_is_no_stop(self) -> None:
        st, entries = self._run({"s1": ["ok", "failed"]}, keep_going=False)
        self.assertIsNone(st.stop)
        self.assertEqual(ms.exit_code(entries, st.stop), 1)

    def test_max_failures_counts_consecutive_per_shard(self) -> None:
        # s1: failed, ok (reset), failed, failed → stop at the 2nd in a row.
        st, entries = self._run({"s1": ["failed", "ok", "failed", "failed", "ok"], "s2": ["failed"] * 5},
                                keep_going=True, max_failures=2)
        self.assertEqual((st.stop.reason, st.stop.shard), ("max_failures", "s2"))
        self.assertIn("not_run", [e.kind for e in entries])

    def test_skipped_does_not_touch_the_counter(self) -> None:
        st, _ = self._run({"s1": ["failed", "skipped", "failed", "ok"]}, keep_going=True, max_failures=2)
        self.assertEqual(str(st.stop), "max_failures:s1")

    def test_shard_conn_error_marks_rest_of_shard_not_run(self) -> None:
        st, entries = self._run({"s1": ["failed", "ok", "ok"], "s2": ["ok", "ok"]}, keep_going=True,
                                max_failures=1, conn={"s1_0": "shard"})
        self.assertIsNone(st.stop)                      # not counted toward --max-failures
        by = {e.target.schema: (e.kind, e.reason) for e in entries}
        self.assertEqual(by["s1_1"], ("not_run", Reason.SHARD_UNREACHABLE))
        self.assertEqual(by["s1_2"], ("not_run", Reason.SHARD_UNREACHABLE))
        self.assertEqual(by["s2_1"][0], "ok")

    def test_default_conn_error_stops_everything(self) -> None:
        st, entries = self._run({"s1": ["failed", "ok"], "s2": ["ok"]}, keep_going=True,
                                conn={"s1_0": "default"})
        self.assertEqual(str(st.stop), "default_unreachable")
        self.assertEqual([e.kind for e in entries][1:], ["not_run", "not_run"])


class ExitCodeTests(SimpleTestCase):
    def test_codes(self) -> None:
        ok, noop, skipped = _entry("a"), _entry("b"), _entry("c")
        ok.kind, noop.kind, skipped.kind = "ok", "noop", "skipped"
        self.assertEqual(ms.exit_code([ok, noop], None), 0)
        self.assertEqual(ms.exit_code([ok, skipped], None), 1)
        self.assertEqual(ms.exit_code([ok], ms.Stop("fail_fast")), 1)
        self.assertEqual(ms.exit_code([], None), 0)


# ---------------------------------------------------------------------------
class _CommandCase(SimpleTestCase):
    """call_command with the migrator's per-target functions and target selection mocked."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-cmd-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def run_cmd(self, *args, targets=(), tenant_results=(), shared_result=None, cli=False,
                **kw) -> tuple[int, str, mock.MagicMock]:
        results = iter(tenant_results)
        tenant = mock.MagicMock(side_effect=lambda pk, opts: next(results))
        out = io.StringIO()
        cmd = load_command_class("tenants", "migrate_schemas")
        cmd._cli = cli
        with mock.patch.object(migrator, "migrate_tenant", tenant), \
             mock.patch.object(migrator, "migrate_shared",
                               return_value=shared_result or _res("ok", "public", "default")), \
             mock.patch(f"{CMD}._select_targets", return_value=list(targets)), \
             mock.patch(f"{CMD}._recheck_locked"), \
             override_settings(TENANT_MIGRATE_LOG_DIR=self.tmp / "logs"):
            try:
                call_command(cmd, *args, stdout=out, stderr=io.StringIO(), **kw)
                code = 0
            except CommandError as e:
                code = e.returncode
        return code, out.getvalue(), tenant


class FlagTests(_CommandCase):
    def assert_refused(self, *args, hint: str, **kw) -> None:
        with self.assertRaises(CommandError) as ctx:
            call_command("migrate_schemas", *args, stdout=io.StringIO(), **kw)
        self.assertIn(hint, str(ctx.exception))
        self.assertEqual(ctx.exception.returncode, 1)

    def test_check_list_plan_point_to_reconcile(self) -> None:
        self.assert_refused("--check", hint="reconcile_tenants --check")
        self.assert_refused("--list", hint="reconcile_tenants --check")
        self.assert_refused("--plan", hint="reconcile_tenants --report --plan")

    def test_executor(self) -> None:
        self.assert_refused("--executor=multiprocessing", hint="not supported")

    def test_maintenance_needs_schema(self) -> None:
        self.assert_refused("--tenant", "--maintenance", hint="only together with -s")

    def test_targeted_needs_one_target(self) -> None:
        self.assert_refused("--fake", hint="exactly one target")
        self.assert_refused("--tenant", "--prune", hint="exactly one target")
        self.assert_refused("users", "0001", hint="exactly one target")

    def test_targeted_with_shared_is_allowed(self) -> None:
        code, _, _ = self.run_cmd("--shared", "--fake")
        self.assertEqual(code, 0)

    def test_jobs_validation(self) -> None:
        self.assert_refused("--jobs=0", hint="--jobs must be")
        self.assert_refused("--max-failures=0", hint="--max-failures must be")
        self.assert_refused("--jobs=2", "--jobs-per-shard=3", hint="must not exceed")
        with override_settings(TENANT_MIGRATE_JOBS=True):
            self.assert_refused(hint="--jobs must be")

    def test_misleading_combinations_are_refused(self) -> None:
        """Review 2026-10-13: these used to "succeed" doing less than asked."""
        self.assert_refused("-s", "public", "--tenant", hint="-s public is the --shared stage")
        self.assert_refused("--jobs-per-shard=2",
                            hint="(1, the TENANT_MIGRATE_JOBS default; pass --jobs as well)")

    def test_max_failures_without_keep_going_warns(self) -> None:
        code, out, _ = self.run_cmd("--tenant", "--max-failures=5",
                                    targets=_targets(("a", "t1")), tenant_results=[_res("ok", "a")])
        self.assertEqual(code, 0)
        self.assertIn("--max-failures is ignored without --keep-going", out)
        code, out, _ = self.run_cmd("--tenant", "--max-failures=5", "--keep-going",
                                    targets=_targets(("a", "t1")), tenant_results=[_res("ok", "a")])
        self.assertNotIn("is ignored", out)

    def test_unknown_database(self) -> None:
        self.assert_refused("--tenant", "--database=nope", hint="not a configured")

    def test_summary_json_path(self) -> None:
        self.assert_refused("--summary-json=-", hint="'-' (stdout) is not allowed")
        self.assert_refused(f"--summary-json={self.tmp}/no/such/dir/x.json", hint="does not exist")

    def test_jobs_above_one_under_call_command_runs_sequentially(self) -> None:
        code, out, tenant = self.run_cmd(
            "--tenant", "--jobs=4",
            targets=_targets(("a", "tenant_1"), ("b", "tenant_2")),
            tenant_results=[_res("ok", "a"), _res("ok", "b", "tenant_2")])
        self.assertEqual(code, 0)
        self.assertIn("only from the command line", out)
        self.assertEqual(tenant.call_count, 2)          # in this process: no fork

    def test_run_from_argv_marks_cli(self) -> None:
        cmd = load_command_class("tenants", "migrate_schemas")
        with mock.patch.object(BaseCommand, "run_from_argv") as base:
            cmd.run_from_argv(["manage.py", "migrate_schemas", "--tenant"])
        base.assert_called_once()
        self.assertTrue(cmd._cli)
        self.assertEqual(cmd._argv, ["--tenant"])
        self.assertFalse(load_command_class("tenants", "migrate_schemas")._cli)


class StagesTests(_CommandCase):
    def test_shared_failure_stops_before_tenants(self) -> None:
        code, out, tenant = self.run_cmd(
            shared_result=_res("failed", "public", "default", reason="locked", msg="shared stage busy"),
            targets=_targets(("a", "tenant_1")))
        self.assertEqual(code, 1)
        tenant.assert_not_called()
        self.assertIn("Stop: shared_failed", out)

    def test_both_stages_ok(self) -> None:
        code, out, tenant = self.run_cmd(targets=_targets(("a", "tenant_1")),
                                         tenant_results=[_res("noop", "a")])
        self.assertEqual(code, 0)
        self.assertIn("ok=1  noop=1  failed=0", out)

    def test_skipped_makes_exit_1(self) -> None:
        code, _, _ = self.run_cmd("--tenant", targets=_targets(("a", "tenant_1")),
                                  tenant_results=[_res("skipped", "a")])
        self.assertEqual(code, 1)

    def test_keyboard_interrupt_propagates_and_marks_in_flight(self) -> None:
        path = self.tmp / "s.json"

        def boom(pk, opts):
            raise KeyboardInterrupt
        cmd = load_command_class("tenants", "migrate_schemas")
        with mock.patch.object(migrator, "migrate_tenant", side_effect=boom), \
             mock.patch(f"{CMD}._select_targets", return_value=_targets(("a", "t1"), ("b", "t1"))), \
             self.assertRaises(KeyboardInterrupt):
            call_command(cmd, "--tenant", f"--summary-json={path}", stdout=io.StringIO())
        doc = json.loads(path.read_text())
        assert_reported_states(self, doc)
        self.assertEqual(doc["exit_code"], 130)
        self.assertEqual(doc["stop"], {"reason": "hard_stop"})
        self.assertEqual([(r["kind"], r["reason"]) for r in doc["results"]],
                         [("interrupted", "hard_stop"), ("not_run", "stopped")])


class SummaryJsonTests(_CommandCase):
    def run_json(self, *args, **kw) -> tuple[int, dict]:
        path = self.tmp / "summary.json"
        code, _, _ = self.run_cmd(*args, f"--summary-json={path}", **kw)
        self.assertFalse(path.with_name("summary.json.tmp").exists())
        return code, json.loads(path.read_text())

    def test_structure(self) -> None:
        code, doc = self.run_json(
            "--keep-going", cli=True,
            targets=_targets(("a", "tenant_1"), ("b", "tenant_1")),
            tenant_results=[_res("failed", "a", reason="lock_timeout", msg="timeout",
                                 invalid_indexes=("i1",)), _res("ok", "b")])
        self.assertEqual(code, 1)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["exit_code"], 1)
        self.assertIsNone(doc["stop"])
        self.assertEqual(doc["options"]["stages"], ["shared", "tenant"])
        self.assertTrue(doc["options"]["keep_going"])
        self.assertEqual(doc["counts"], {"ok": 2, "noop": 0, "failed": 1, "skipped": 0,
                                         "not_run": 0, "interrupted": 0})
        self.assertEqual(doc["results"][0]["stage"], "shared")
        a = doc["results"][1]
        self.assertEqual((a["schema"], a["tenant_id"], a["status_before"], a["kind"], a["reason"],
                          a["invalid_indexes"], a["rechecked"]),
                         ("a", 1, "active", "failed", "lock_timeout", ["i1"], False))
        self.assertIsNotNone(doc["log_dir"])
        self.assertRegex(doc["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_stop_and_not_run(self) -> None:
        code, doc = self.run_json("--tenant", targets=_targets(("a", "t1"), ("b", "t1")),
                                  tenant_results=[_res("failed", "a", "t1")])
        self.assertEqual(doc["stop"], {"reason": "fail_fast"})
        self.assertEqual(doc["results"][1]["kind"], "not_run")
        self.assertEqual(doc["results"][1]["reason"], "stopped")
        self.assertEqual(doc["options"]["stages"], ["tenant"])
        self.assertIsNone(doc["argv"])                  # call_command, not the CLI
        self.assertIsNone(doc["log_dir"])

    def test_max_failures_stop_names_the_shard(self) -> None:
        _, doc = self.run_json("--tenant", "--keep-going", "--max-failures=1",
                               targets=_targets(("a", "t1"), ("b", "t1")),
                               tenant_results=[_res("failed", "a", "t1")])
        self.assertEqual(doc["stop"], {"reason": "max_failures", "shard": "t1"})

    def test_overwrites_previous_file(self) -> None:
        path = self.tmp / "summary.json"
        path.write_text("old")
        self.run_json("--shared")
        self.assertEqual(json.loads(path.read_text())["version"], 1)

    def test_reason_codes_match_the_plan(self) -> None:
        self.assertEqual(set(Reason.all()), {
            "applied", "resumed", "targeted", "up_to_date", "done_by_another_run",
            "provisioning_elsewhere", "locked", "status_failed", "status_maintenance",
            "status_changed", "lease_lost", "tenant_deleted", "code_older_than_db",
            "schema_missing", "schema_exists", "lock_timeout", "migration_error",
            "incomplete_after_run", "conn_error", "default_shard_anomaly",
            "shard_unreachable", "stopped", "hard_stop"})


# ---------------------------------------------------------------------------
class RunLogTests(_CommandCase):
    """File-system half of §9.9; the real per-tenant files are in db_migrate_schemas.py."""

    def test_retention_keeps_two_previous(self) -> None:
        root = self.tmp / "logs"
        names = ["2026-10-01T00-00-00Z_h_1", "2026-10-02T00-00-00Z_h_2",
                 "2026-10-03T00-00-00Z_h_3", "2026-10-04T00-00-00Z_h_4"]
        for n in names:
            (root / n).mkdir(parents=True)
        (root / "keep-me").mkdir()
        from datetime import datetime, timezone
        run = migrator.start_run_dir(root, datetime(2026, 10, 5, tzinfo=timezone.utc))
        self.assertEqual(sorted(p.name for p in root.iterdir()),
                         sorted([*names[2:], "keep-me", run.name]))
        self.assertRegex(run.name, r"^2026-10-05T00-00-00Z_.+_\d+$")

    def test_cli_writes_summary_log_and_prints_logs_line(self) -> None:
        code, out, _ = self.run_cmd("--shared", cli=True)
        self.assertEqual(code, 0)
        runs = list((self.tmp / "logs").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertIn(f"Logs: {runs[0]}", out)
        self.assertIn("ok", (runs[0] / "_summary.log").read_text())

    def test_dead_live_stream_never_breaks_the_tee(self) -> None:
        """Ctrl-C reaches the whole process group and kills `| tee`: writes to the live
        stream raise BrokenPipeError. The tee must swallow that and keep the file."""
        class Dead(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")
        path = self.tmp / "dead.log"
        dead = Dead()
        with mock.patch.object(sys, "stdout", dead), mock.patch.object(sys, "stderr", dead):
            log = migrator._TargetLog(path, {"schema": "acme"})
            print("  Applying users.0001_initial... OK")            # must not raise
            sys.stdout.flush()
            log.close("result: ok")
        self.assertIn("\n  Applying users.0001_initial... OK\n", path.read_text())

    def test_tenant_log_name(self) -> None:
        self.assertEqual(migrator.tenant_log_name("acme", 7), "acme.log")
        self.assertEqual(migrator.tenant_log_name("summary", 7), "summary.log")   # its own file
        for bad in ("../evil", "a/b", "_summary", "_shared", "Upper", "pg_x", "a b"):
            self.assertEqual(migrator.tenant_log_name(bad, 7), "_tenant_7.log", bad)
        self.assertTrue(migrator.SUMMARY_LOG.startswith("_") and migrator.SHARED_LOG.startswith("_"))

    def test_call_command_writes_no_files(self) -> None:
        self.run_cmd("--shared")
        self.assertFalse((self.tmp / "logs").exists())

    def test_tee_strips_prefix_and_colour_in_the_file_only(self) -> None:
        path = self.tmp / "x.log"
        live = io.StringIO()
        with mock.patch.object(sys, "stdout", live), mock.patch.object(sys, "stderr", io.StringIO()):
            log = migrator._TargetLog(path, {"schema": "acme"})
            print("[\x1b[33mstandard\x1b[0m:\x1b[33macme\x1b[0m]   Applying users.0001_initial... OK")
            sys.stdout.write("[1/2 (50%) standard:acme] partial")
            log.close("result: ok")
            self.assertIs(sys.stdout, live)             # restored
        self.assertIn("[\x1b[33mstandard", live.getvalue())
        text = path.read_text()
        self.assertIn("\n  Applying users.0001_initial... OK\n", text)
        self.assertIn("\npartial\n", text)
        self.assertTrue(text.startswith("schema: acme\n"))
        self.assertTrue(text.rstrip().endswith("result: ok"))


# ---------------------------------------------------------------------------
SNAP = {"status": "pending", "behind": 3, "lease_owner": "h:1:2026-10-12T00:00:00Z"}


REPORTED_KINDS = {"ok", "noop", "failed", "skipped", "not_run", "interrupted"}


def assert_reported_states(case: SimpleTestCase, doc: dict) -> None:
    """No internal state may leak into the report: every row carries a reported kind and a
    real reason code (an in-flight entry used to be marked through `reason`)."""
    for r in doc["results"]:
        case.assertIn(r["kind"], REPORTED_KINDS, r)
        case.assertIn(r["reason"], set(Reason.all()), r)


class _SignalCase(_CommandCase):
    """Runs `fn(pk, opts)` as migrate_tenant; signal notes and snapshots are mocked."""

    def run_fn(self, fn, *args, n: int = 4, cli: bool = True) -> tuple[int, dict, str]:
        path = self.tmp / "s.json"
        targets = _targets(*[(f"t{i}", f"tenant_{1 + (i - 1) % 2}") for i in range(1, n + 1)])
        out = io.StringIO()
        cmd = load_command_class("tenants", "migrate_schemas")
        cmd._cli = cli
        before = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        with mock.patch.object(migrator, "migrate_tenant", side_effect=fn), \
             mock.patch.object(migrator, "snapshot", return_value=SNAP) as snap, \
             mock.patch(f"{CMD}._select_targets", return_value=targets), \
             mock.patch(f"{CMD}._recheck_locked"), \
             mock.patch(f"{CMD}._signal_note"), \
             override_settings(TENANT_MIGRATE_LOG_DIR=self.tmp / "logs"):
            try:
                call_command(cmd, "--tenant", *args, f"--summary-json={path}",
                             stdout=out, stderr=io.StringIO())
                code = 0
            except CommandError as e:
                code = e.returncode
        self.assertEqual((signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)),
                         before, "handlers not restored")
        self.snap_calls = [c.args[0] for c in snap.call_args_list]
        doc = json.loads(path.read_text())
        assert_reported_states(self, doc)
        return code, doc, out.getvalue()

    @staticmethod
    def kinds(doc: dict) -> dict[str, tuple[str, str]]:
        return {r["schema"]: (r["kind"], r["reason"]) for r in doc["results"]}


def _ok(pk: int, msg: str = "") -> Result:
    return Result("ok", f"t{pk}", f"tenant_{1 + (pk - 1) % 2}", "applied", msg)


def _pdeathsig() -> int:
    v = ctypes.c_int()
    ctypes.CDLL(None).prctl(2, ctypes.byref(v))         # PR_GET_PDEATHSIG
    return v.value


class ParallelTests(_SignalCase):
    def test_workers_are_forked_and_initialised(self) -> None:
        def fn(pk, opts):
            ign = signal.getsignal(signal.SIGINT) is signal.SIG_IGN
            return _ok(pk, f"{os.getpid()} {ign} {_pdeathsig()} {sys.stdout.line_buffering}")
        code, doc, out = self.run_fn(fn, "--jobs=2")
        self.assertEqual(code, 0, out)
        rows = [r["msg"].split() for r in doc["results"]]
        pids = {int(r[0]) for r in rows}
        self.assertNotIn(os.getpid(), pids)
        self.assertLessEqual(len(pids), 2)
        self.assertEqual({tuple(r[1:]) for r in rows}, {("True", str(int(signal.SIGKILL)), "True")})
        self.assertEqual(doc["options"]["jobs"], 2)

    def test_jobs_per_shard_limit(self) -> None:
        """--jobs=4 --jobs-per-shard=1 on 2 shards: never two tenants of one shard at once."""
        marks = Path(self.tmp / "marks")
        marks.mkdir()

        def fn(pk, opts):
            shard = 1 + (pk - 1) % 2
            mine = marks / f"{shard}-{pk}"
            mine.touch()
            time.sleep(0.3)
            busy = [p.name for p in marks.iterdir() if p.name.startswith(f"{shard}-")]
            mine.unlink()
            return _ok(pk, str(len(busy)))
        code, doc, _ = self.run_fn(fn, "--jobs=4", "--jobs-per-shard=1", n=6)
        self.assertEqual(code, 0)
        self.assertEqual({r["msg"] for r in doc["results"]}, {"1"})

    def test_worker_exception_is_a_failed_target(self) -> None:
        def fn(pk, opts):
            if pk == 1:
                raise ValueError("boom")
            return _ok(pk)
        code, doc, _ = self.run_fn(fn, "--jobs=2", "--keep-going")
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(doc)["t1"], ("failed", "migration_error"))
        self.assertEqual(doc["results"][0]["msg"], "ValueError: boom")
        self.assertEqual(doc["counts"]["ok"], 3)

    def test_first_signal_drains(self) -> None:
        def fn(pk, opts):
            if pk == 1:
                os.kill(os.getppid(), signal.SIGINT)
            time.sleep(1.0)
            return _ok(pk)
        code, doc, out = self.run_fn(fn, "--jobs=2", n=6)
        self.assertEqual(code, 1)
        self.assertEqual(doc["stop"], {"reason": "signal"})
        k = self.kinds(doc)
        self.assertEqual((k["t1"], k["t2"]), (("ok", "applied"), ("ok", "applied")))
        self.assertEqual({k[f"t{i}"] for i in range(3, 7)}, {("not_run", "stopped")})
        self.assertEqual(self.snap_calls, [])
        self.assertIn("Stop: signal", out)

    def test_second_signal_kills_workers_and_snapshots(self) -> None:
        def fn(pk, opts):
            if pk == 1:
                os.kill(os.getppid(), signal.SIGINT)
                time.sleep(0.5)                         # two signals must not coalesce
                os.kill(os.getppid(), signal.SIGTERM)
            time.sleep(60)
            return _ok(pk)
        started = time.monotonic()
        code, doc, out = self.run_fn(fn, "--jobs=2")
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(code, 1)
        self.assertEqual(doc["stop"], {"reason": "hard_stop"})
        k = self.kinds(doc)
        self.assertEqual((k["t1"], k["t2"]), (("interrupted", "hard_stop"),) * 2)
        self.assertEqual((k["t3"], k["t4"]), (("not_run", "stopped"),) * 2)
        self.assertEqual(doc["results"][0]["snapshot"], SNAP)
        self.assertNotIn("snapshot", doc["results"][2])
        self.assertEqual(sorted(self.snap_calls), [1, 2])
        self.assertEqual(doc["counts"]["interrupted"], 2)
        self.assertIn("t1: status=pending; 3 migration(s) behind; lease held by h:1:", out)
        self.assertIn("migration_lease --release t1", out)
        import multiprocessing
        self.assertEqual(multiprocessing.active_children(), [])

    def test_dead_worker_breaks_the_pool(self) -> None:
        def fn(pk, opts):
            if pk == 1:
                os.kill(os.getpid(), signal.SIGKILL)
            time.sleep(2)
            return _ok(pk)
        code, doc, _ = self.run_fn(fn, "--jobs=2", "--keep-going")
        self.assertEqual(code, 1)
        self.assertEqual(doc["stop"], {"reason": "broken_pool"})
        k = self.kinds(doc)
        self.assertEqual(k["t1"], ("interrupted", "hard_stop"))
        self.assertIn("BrokenProcessPool", doc["results"][0]["msg"])
        self.assertIn(1, self.snap_calls)

    def test_single_target_is_not_forked(self) -> None:
        code, doc, _ = self.run_fn(lambda pk, opts: _ok(pk, str(os.getpid())), "--jobs=2", n=1)
        self.assertEqual((code, doc["results"][0]["msg"]), (0, str(os.getpid())))


class SignalTests(_SignalCase):
    """Sequential mode (--jobs=1): the handler runs in this very process."""

    def test_first_signal_drains(self) -> None:
        def fn(pk, opts):
            if pk == 1:
                os.kill(os.getpid(), signal.SIGINT)
            return _ok(pk)
        code, doc, _ = self.run_fn(fn, n=3)
        self.assertEqual(code, 1)
        self.assertEqual(doc["stop"], {"reason": "signal"})
        self.assertEqual([r["kind"] for r in doc["results"]], ["ok", "not_run", "not_run"])

    def test_second_signal_is_a_hard_stop_with_exit_1(self) -> None:
        def fn(pk, opts):
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGINT)         # → KeyboardInterrupt right here
            return _ok(pk)
        code, doc, out = self.run_fn(fn, n=3)
        self.assertEqual((code, doc["exit_code"]), (1, 1))
        self.assertEqual(doc["stop"], {"reason": "hard_stop"})
        self.assertEqual([(r["kind"], r["reason"]) for r in doc["results"]],
                         [("interrupted", "hard_stop"), ("not_run", "stopped"), ("not_run", "stopped")])
        self.assertEqual(doc["results"][0]["snapshot"], SNAP)
        self.assertEqual(self.snap_calls, [1])
        self.assertIn("Interrupted targets", out)

    def test_signal_during_shared_skips_the_tenant_stage(self) -> None:
        def shared(opts):
            os.kill(os.getpid(), signal.SIGTERM)
            return _res("ok", "public", "default")
        with mock.patch.object(migrator, "migrate_shared", side_effect=shared):
            code, doc, _ = self.run_fn(lambda pk, opts: _ok(pk), "--shared", n=0)
        # --shared and --tenant together: run_fn adds --tenant
        self.assertEqual(code, 1)
        self.assertEqual(doc["stop"], {"reason": "signal"})
        self.assertEqual([r["schema"] for r in doc["results"]], ["public"])

    def test_call_command_leaves_sigint_alone(self) -> None:
        seen = []

        def fn(pk, opts):
            seen.append(signal.getsignal(signal.SIGINT))
            return _ok(pk)
        before = signal.getsignal(signal.SIGINT)
        code, _, _ = self.run_fn(fn, n=1, cli=False)
        self.assertEqual((code, seen), (0, [before]))


# ---------------------------------------------------------------------------
class LegacyCommandTests(SimpleTestCase):
    def test_found_is_tenant_command_and_marked_legacy(self) -> None:
        cmd = load_command_class("tenants", "migrate_schemas_legacy")
        self.assertIsInstance(cmd, TenantCommand)
        self.assertTrue(cmd.help.startswith("LEGACY:"))
        mod = sys.modules[type(cmd).__module__]
        self.assertTrue(mod.__doc__.startswith("LEGACY:"))

    def test_nothing_calls_it(self) -> None:
        root = Path(__file__).resolve().parents[2]
        pat = re.compile(r"""call_command\(\s*["']migrate_schemas_legacy["']""")
        hits = [str(p) for p in root.rglob("*.py")
                if "p_env" not in p.parts and pat.search(p.read_text(encoding="utf-8", errors="ignore"))]
        self.assertEqual(hits, [])


class DeadStdoutTests(_CommandCase):
    """The command's own stdout dies mid-run (`| tee` killed): the run still finishes, the
    exit code is unchanged and --summary-json is written (it used to be written AFTER the
    screen summary, so a BrokenPipeError there lost it)."""

    def test_summary_json_survives_a_dead_stdout(self) -> None:
        class Dead(io.StringIO):
            def write(self, s):
                raise BrokenPipeError(32, "Broken pipe")
        path = self.tmp / "s.json"
        with mock.patch.object(migrator, "migrate_tenant",
                               side_effect=[_res("ok", "a"), _res("failed", "b")]), \
             mock.patch(f"{CMD}._select_targets", return_value=_targets(("a", "t1"), ("b", "t1"))), \
             mock.patch(f"{CMD}._recheck_locked"):
            with self.assertRaises(CommandError) as ctx:
                call_command("migrate_schemas", "--tenant", f"--summary-json={path}",
                             stdout=Dead(), stderr=Dead())
        self.assertEqual(ctx.exception.returncode, 1)
        doc = json.loads(path.read_text())
        self.assertEqual([(r["schema"], r["kind"]) for r in doc["results"]], [("a", "ok"), ("b", "failed")])
        self.assertEqual(doc["exit_code"], 1)
