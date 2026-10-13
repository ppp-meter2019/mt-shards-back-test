"""migrate_schemas — zero-downtime tenant migrator (deploy/migrations_plan_rev7.md §9).
Overrides the django-tenants command: `tenants` must be listed BEFORE `django_tenants` in
SHARED_APPS so that Django's command resolution picks this class.

The per-target work is tenants.migrator.migrate_tenant() / migrate_shared(); this command
selects targets, schedules them, applies --keep-going / --max-failures, re-checks
`locked` skips, prints the summary and sets the exit code.

  (no flags)            --shared, then --tenant (a --shared failure stops the run)
  --shared              public on default, under lease 0; busy → fails at once
  --tenant [--database X]
                        every NEW/PENDING/ACTIVE/DEACTIVATED tenant on the active
                        non-default shards (only shard X with --database)
  -s X [--maintenance]  one tenant, whatever its status (FAILED → skipped; MAINTENANCE
                        only with --maintenance)

Exit code 0: every target ok/noop (after the `locked` re-check) and the run was not
stopped; 1: anything else (a CommandError under call_command). Argparse errors exit 2.

Per-target log files (§9.9), the parallel mode (§9.6) and the signal handlers (§9.7)
exist only when run from the CLI (`run_from_argv`); call_command — Celery,
bootstrap_tenant, tests — runs sequentially, writes no files and leaves SIGINT alone.
--summary-json=PATH (§9.10) works on both paths.

Parallel mode (CLI, --jobs > 1, more than one tenant): a fork ProcessPoolExecutor; each
worker migrates one tenant at a time, ignores SIGINT/SIGTERM and dies with the parent
(PR_SET_PDEATHSIG). Signals (CLI, main thread):
  1st SIGINT/SIGTERM    drain: no new targets, in-flight ones finish → Stop: signal
  2nd, parallel         SIGKILL to every worker; leases are kept     → Stop: hard_stop
  2nd, sequential       KeyboardInterrupt in the running tenant, lease rule of §6.5
After a hard stop or a broken pool every in-flight target gets a DB snapshot (status,
migrations behind, lease owner) in the summary and in --summary-json.

Refused with a hint: --check/--list (→ reconcile_tenants --check), --plan
(→ reconcile_tenants --report --plan), --executor other than `standard`.
"""
from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import signal
import socket
import sys
import threading
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from django.conf import settings
from django.core.management.base import CommandError, CommandParser
from django.db import connections
from django_tenants.management.commands import SyncCommon
from django_tenants.management.commands.migrate_schemas import (
    MigrateSchemasCommand as UpstreamCommand,
)
from django_tenants.utils import get_public_schema_name, get_tenant_migration_order

from commons.platform.commands import TenantCommand
from tenants import migrator
from tenants.migrator import MISSING, Reason, Result, RunOptions
from tenants.models import Shard, Tenant

SUMMARY_VERSION = 1
_POLL_SECS = 0.2                    # parallel loop: how soon a drain request is noticed
_PR_SET_PDEATHSIG = 1               # <linux/prctl.h>


# ---------------------------------------------------------------------------
# Scheduling (§9.4) — pure, DB-free
# ---------------------------------------------------------------------------

def pick_next(queues: Mapping[str, Sequence[Any]], inflight: Mapping[str, int],
              jobs: int, per_shard: int, after: str | None) -> str | None:
    """The shard to take the next target from, or None. Shards round-robin in sorted
    order starting after `after` (the shard served last); FIFO within a shard is the
    caller's popleft(). Respects the global `jobs` and the per-shard limit."""
    if sum(inflight.values()) >= jobs:
        return None
    order = sorted(queues)
    if after in order:
        i = order.index(after) + 1
        order = order[i:] + order[:i]
    for shard in order:
        if queues[shard] and inflight.get(shard, 0) < per_shard:
            return shard
    return None


@dataclass
class Target:
    stage: str                          # "shared" | "tenant"
    schema: str
    shard: str
    tenant_id: int | None = None
    status_before: str | None = None


@dataclass
class Entry:
    """One row of the summary: a target plus what happened to it."""
    target: Target
    kind: str = "not_run"               # ok/noop/failed/skipped/not_run/interrupted;
                                        # "running" while its target runs (never reported)
    reason: str = ""
    msg: str = ""
    secs: float = 0.0
    invalid_indexes: tuple[str, ...] = ()
    conn_error: str | None = None
    rechecked: bool = False
    log_file: str | None = None
    snapshot: dict[str, Any] | None = None   # interrupted only (§9.7)

    def take(self, res: Result) -> None:
        self.kind, self.reason, self.msg, self.secs = res.kind, res.reason, res.msg, res.secs
        self.invalid_indexes, self.conn_error = res.invalid_indexes, res.conn_error


@dataclass
class Stop:
    reason: str                         # fail_fast/max_failures/default_unreachable/...
    shard: str | None = None

    def __str__(self) -> str:
        return f"{self.reason}:{self.shard}" if self.shard else self.reason


@dataclass
class TenantStage:
    """The tenant-stage state of §9.4: per-shard FIFO queues, the in-flight count, the
    consecutive-failure counters (in COMPLETION order, Q4) and the stop flag."""
    keep_going: bool
    max_failures: int
    queues: dict[str, deque[Entry]] = field(default_factory=dict)
    inflight: Counter = field(default_factory=Counter)
    consecutive: Counter = field(default_factory=Counter)
    stop: Stop | None = None
    last: str | None = None

    @classmethod
    def of(cls, entries: Iterable[Entry], keep_going: bool, max_failures: int) -> TenantStage:
        st = cls(keep_going, max_failures)
        for e in entries:
            st.queues.setdefault(e.target.shard, deque()).append(e)
        return st

    def remaining(self) -> bool:
        return any(self.queues.values())

    def next(self, jobs: int, per_shard: int) -> Entry | None:
        if self.stop is not None:
            return None
        shard = pick_next(self.queues, self.inflight, jobs, per_shard, self.last)
        if shard is None:
            return None
        self.last = shard
        self.inflight[shard] += 1
        return self.queues[shard].popleft()

    def done(self, e: Entry) -> None:
        shard = e.target.shard
        self.inflight[shard] -= 1
        if e.conn_error == "default":
            self.halt(Stop("default_unreachable"))
            return
        if e.conn_error == "shard":                     # rest of the shard: not run, not counted
            for rest in self.queues[shard]:
                rest.kind, rest.reason, rest.msg = "not_run", Reason.SHARD_UNREACHABLE, "shard unreachable"
            self.queues[shard].clear()
        elif e.kind == "failed":
            self.consecutive[shard] += 1
        elif e.kind in ("ok", "noop"):
            self.consecutive[shard] = 0
        if e.kind != "failed" or self.stop is not None:
            return
        if not self.keep_going:
            if self.remaining():
                self.halt(Stop("fail_fast"))
        elif self.consecutive[shard] >= self.max_failures:
            self.halt(Stop("max_failures", shard))

    def halt(self, stop: Stop, override: bool = False) -> None:
        """Stop taking targets; the queued ones become not_run. The first reason stays
        (a failure while draining keeps `signal`) unless `override` (a hard stop)."""
        if self.stop is None or override:
            self.stop = stop
        for q in self.queues.values():
            for rest in q:
                rest.kind, rest.reason, rest.msg = "not_run", Reason.STOPPED, f"not run (Stop: {self.stop})"
            q.clear()



def exit_code(entries: Sequence[Entry], stop: Stop | None) -> int:
    return 0 if stop is None and all(e.kind in ("ok", "noop") for e in entries) else 1


# ---------------------------------------------------------------------------
# Fork-pool workers (§9.6) — module level: the pool pickles them by reference
# ---------------------------------------------------------------------------

def _worker_init(parent_pid: int) -> None:
    """Runs once in each forked worker. Ctrl-C hits the whole foreground process group:
    workers ignore it and leave the decision to the parent (§9.7). PDEATHSIG: a worker
    must not outlive a SIGKILLed parent and go on migrating unsupervised; the getppid()
    check closes the race where the parent died before prctl. Line buffering keeps the
    workers' lines whole on the shared stdout."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except (AttributeError, OSError):                   # not Linux
        pass
    if os.getppid() != parent_pid:
        os._exit(1)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)


def _work(pk: int, opts: RunOptions) -> Result:
    return migrator.migrate_tenant(pk, opts)


def _kill_workers() -> int:
    """SIGKILL every child of this process (m7: active_children(), not ex._processes)."""
    n = 0
    for p in multiprocessing.active_children():
        try:
            os.kill(p.pid, signal.SIGKILL)
            n += 1
        except (ProcessLookupError, TypeError):         # already gone / never started
            pass
    return n


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

TARGETED_OPTIONS = ("fake", "fake_initial", "prune", "run_syncdb", "app_label", "migration_name")


# TenantCommand FIRST in the MRO: its execute() runs the USE_MULTITENANT guard, then
# continues into the normal BaseCommand flow, which ends in our handle().
class Command(TenantCommand, UpstreamCommand):
    help = ("Zero-downtime tenant migrator: --shared, then --tenant, under per-tenant leases. "
            "See deploy/migrations_plan_rev7.md §9 and the module docstring.")

    _cli = False                            # set by run_from_argv: log files, parallel mode
    _argv: list[str] | None = None

    def run_from_argv(self, argv: list[str]) -> None:
        self._cli = True
        self._argv = list(argv[2:])          # after "manage.py migrate_schemas"
        super().run_from_argv(argv)

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        # Strip the upstream default of --database='default' so "not specified" differs
        # from "explicitly default" (checked by test_upstream_contract).
        for action in parser._actions:
            if action.dest == "database":
                action.default = None
                action.help = ("Shard alias. --tenant: only tenants on this shard. "
                               "-s: must match the tenant's shard. Ignored for --shared.")
                break
        parser.add_argument("--jobs", type=int, default=None,
                            help="Tenants migrated at once (default TENANT_MIGRATE_JOBS).")
        parser.add_argument("--jobs-per-shard", type=int, default=None, dest="jobs_per_shard",
                            help="Of those, at most this many per shard "
                                 "(default TENANT_MIGRATE_JOBS_PER_SHARD).")
        parser.add_argument("--keep-going", action="store_true", dest="keep_going",
                            help="Do not stop at the first failure; stop when one shard has "
                                 "--max-failures failures in a row.")
        parser.add_argument("--max-failures", type=int, default=None, dest="max_failures",
                            help="With --keep-going: consecutive failures on one shard that "
                                 "stop the whole run (default 3).")
        parser.add_argument("--maintenance", action="store_true",
                            help="With -s only: allow migrating a MAINTENANCE tenant.")
        parser.add_argument("--summary-json", dest="summary_json", default=None, metavar="PATH",
                            help="Also write a machine-readable run summary to PATH (§9.10).")

    # ------------------------------------------------------------------
    def handle(self, *args: Any, **options: Any) -> None:
        # Upstream flag parsing (sets sync_public/sync_tenant/schema_name/executor).
        SyncCommon.handle(self, *args, **options)
        public = get_public_schema_name()
        if self.schema_name == public:
            if options.get("tenant"):           # upstream would quietly drop the tenant stage
                raise CommandError("-s public is the --shared stage; use --shared, or --tenant "
                                   "without -s.")
            self.schema_name, self.sync_public, self.sync_tenant = None, True, False

        self._validate(options)
        opts = self._run_options(options)
        self.verbosity = int(options.get("verbosity", 1))
        self.started_at = datetime.now(dt_timezone.utc)
        self.entries: list[Entry] = []
        self.stop: Stop | None = None
        self.log_dir: Path | None = None
        self._signals_seen = 0
        self._hard = False                  # set by the 2nd signal (§9.7)
        self._pool_live = False             # the 2nd signal kills workers instead of raising
        code: int | None = None
        try:
            if self._cli:
                try:
                    self.log_dir = migrator.start_run_dir(
                        Path(settings.TENANT_MIGRATE_LOG_DIR), self.started_at)
                except OSError as exc:
                    raise CommandError(f"TENANT_MIGRATE_LOG_DIR: cannot create the run folder: {exc}")
            with self._signal_handlers():
                self._run(replace(opts, log_dir=self.log_dir))
            code = exit_code(self.entries, self.stop)
        except KeyboardInterrupt:
            # Our 2nd signal in sequential mode (→ code 1, like any stopped run), or a
            # foreign Ctrl-C under call_command, which propagates (130). Either way
            # migrate_tenant's finally has already applied the lease rule of §6.5.
            code = 1 if self._hard else 130
            self.stop = Stop("hard_stop")
            self._snapshots(self._settle("interrupted", Reason.HARD_STOP, "interrupted (hard stop)"))
            if not self._hard:
                raise
        except Exception as exc:
            code = 1
            self._settle("failed", Reason.MIGRATION_ERROR, f"{type(exc).__name__}: {exc}"[:500])
            raise
        finally:
            if code is not None:
                self._report(code, options)
        if code:
            raise CommandError(self._error_line(), returncode=code)

    def _settle(self, kind: str, reason: str, msg: str) -> list[Entry]:
        """After an escaping exception: the running entries get `kind` (and are returned),
        unstarted ones become not_run."""
        inflight = []
        for e in self.entries:
            if e.kind == "running":
                e.kind, e.reason, e.msg = kind, reason, msg
                inflight.append(e)
            elif e.kind == "not_run" and not e.reason:
                e.reason, e.msg = Reason.STOPPED, f"not run (Stop: {self.stop or 'error'})"
        return inflight

    # ------------------------------------------------------------------
    # Signals (§9.7)
    # ------------------------------------------------------------------
    @contextmanager
    def _signal_handlers(self) -> Iterator[None]:
        """CLI and main thread only; the previous handlers come back in finally."""
        if not self._cli or threading.current_thread() is not threading.main_thread():
            yield
            return
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        for s in saved:
            signal.signal(s, self._on_signal)
        try:
            yield
        finally:
            for s, h in saved.items():
                signal.signal(s, h)

    def _on_signal(self, signum: int, frame: Any) -> None:
        """Only flags and os.write here: a handler may interrupt the main thread inside a
        print or a deque operation. The loops act on `_signals_seen` themselves."""
        self._signals_seen += 1
        name = signal.Signals(signum).name
        if self._signals_seen == 1:
            self._signal_note(f"\n{name}: draining — no new targets, in-flight ones finish. "
                              f"Send it again for a hard stop.\n")
            return
        self._hard = True
        if self._pool_live:
            n = _kill_workers()
            self._signal_note(f"\n{name} again: hard stop — {n} worker(s) killed, their "
                              f"leases are kept (see the snapshot in the summary).\n")
            return
        self._signal_note(f"\n{name} again: hard stop.\n")
        raise KeyboardInterrupt

    @staticmethod
    def _signal_note(text: str) -> None:
        try:
            os.write(2, text.encode("utf-8", "replace"))   # raw fd: safe inside a handler
        except OSError:
            pass

    def _drain_requested(self) -> bool:
        return self._signals_seen > 0

    def _snapshots(self, entries: Iterable[Entry]) -> None:
        """§9.7: the DB state of every target cut off by a hard stop or a broken pool."""
        for e in entries:
            try:
                e.snapshot = migrator.snapshot(e.target.tenant_id or 0)
            except KeyboardInterrupt:                   # yet another Ctrl-C: skip the rest
                break

    # ------------------------------------------------------------------
    def _validate(self, options: dict[str, Any]) -> None:
        if options.get("check_unapplied") or options.get("list"):
            raise CommandError("migrate_schemas --check/--list is not supported; "
                               "use `reconcile_tenants --check`.")
        if options.get("plan"):
            raise CommandError("migrate_schemas --plan is not supported; "
                               "use `reconcile_tenants --report --plan`.")
        if self.executor not in (None, "standard"):
            raise CommandError(f"--executor={self.executor!r} is not supported: the migrator "
                               f"runs its own scheduler (--jobs / --jobs-per-shard).")

        jobs_given = options.get("jobs") is not None
        jobs = options["jobs"] if jobs_given else settings.TENANT_MIGRATE_JOBS
        per_shard = (options["jobs_per_shard"] if options.get("jobs_per_shard") is not None
                     else settings.TENANT_MIGRATE_JOBS_PER_SHARD)
        max_failures_given = options.get("max_failures") is not None
        if not max_failures_given:              # both dicts: SyncCommon kept its own copy
            options["max_failures"] = self.options["max_failures"] = 3
        for name, v in (("--jobs", jobs), ("--jobs-per-shard", per_shard),
                        ("--max-failures", options["max_failures"])):
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise CommandError(f"{name} must be an integer >= 1 (got {v!r}).")
        if per_shard > jobs:
            src = "" if jobs_given else ", the TENANT_MIGRATE_JOBS default; pass --jobs as well"
            raise CommandError(f"--jobs-per-shard ({per_shard}) must not exceed --jobs "
                               f"({jobs}{src}).")
        self.jobs, self.per_shard = jobs, per_shard
        if max_failures_given and not options.get("keep_going"):
            self._out(self.stdout, self.style.WARNING(
                "--max-failures is ignored without --keep-going: the run stops at the first "
                "failure."))

        if options.get("maintenance") and not self.schema_name:
            raise CommandError("--maintenance is allowed only together with -s <schema>.")

        path = options.get("summary_json")
        if path is not None:
            if path == "-":
                raise CommandError("--summary-json needs a file path; '-' (stdout) is not "
                                   "allowed: stdout carries the migration output.")
            parent = Path(path).resolve().parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                raise CommandError(f"--summary-json: folder {str(parent)!r} does not exist "
                                   f"or is not writable.")

        database = options.get("database")
        if database and database not in connections.databases:
            raise CommandError(f"--database={database!r} is not a configured database alias.")
        if database and self.sync_tenant and not self.schema_name:
            # --tenant --database X must not "succeed" by selecting nothing.
            shard = Shard.objects.filter(alias=database).first()
            if shard is None:
                raise CommandError(f"--database={database!r}: no Shard is registered for this "
                                   f"alias (run `sync_shards`).")
            if shard.is_default:
                raise CommandError(f"--database={database!r}: the default shard holds only "
                                   f"public, no tenants; for public use --shared.")
            if not shard.is_active:
                raise CommandError(f"--database={database!r}: shard is inactive and --tenant "
                                   f"skips inactive shards. Migrate its tenants one by one with "
                                   f"-s <schema> (list them: reconcile_tenants --report).")

        targeted = any(options.get(k) for k in TARGETED_OPTIONS)
        self.tenant = None
        if self.schema_name:
            try:
                self.tenant = Tenant.objects.select_related("shard").get(schema_name=self.schema_name)
            except Tenant.DoesNotExist:
                raise CommandError(f"Tenant {self.schema_name!r} not found.")
            if database and database != self.tenant.shard.alias:
                raise CommandError(f"--database={database!r} does not match tenant "
                                   f"{self.schema_name!r} (its shard is "
                                   f"{self.tenant.shard.alias!r}).")
            if targeted and self.tenant.status in migrator.PROVISIONABLE:
                raise CommandError(f"Targeted flags ({', '.join(TARGETED_OPTIONS)}) are not "
                                   f"allowed for a {self.tenant.status} tenant: provisioning "
                                   f"always applies every migration.")
        elif targeted and (self.sync_tenant or not self.sync_public):
            raise CommandError("Targeted flags (--fake, --fake-initial, --prune, --run-syncdb, "
                               "app_label, migration_name) need exactly one target: "
                               "-s <schema> or --shared.")

    def _run_options(self, options: dict[str, Any]) -> RunOptions:
        return RunOptions(
            verbosity=int(options.get("verbosity", 1)),
            fake=bool(options.get("fake")), fake_initial=bool(options.get("fake_initial")),
            prune=bool(options.get("prune")), run_syncdb=bool(options.get("run_syncdb")),
            app_label=options.get("app_label"), migration_name=options.get("migration_name"),
            maintenance=bool(options.get("maintenance")),
        )

    # ------------------------------------------------------------------
    def _run(self, opts: RunOptions) -> None:
        options = self.options
        if self.sync_public:
            if options.get("database") not in (None, "default"):
                self._say(f"--database={options['database']!r} ignored for --shared; using 'default'.")
            e = Entry(Target("shared", "public", "default"))
            self.entries.append(e)
            e.kind = "running"
            e.take(migrator.migrate_shared(opts))
            self._log_file(e, migrator.SHARED_LOG)
            self._line(e)
            if e.kind != "ok":
                self.stop = Stop("shared_failed")
                return
        if not self.sync_tenant:
            return
        if self._drain_requested():
            self.stop = Stop("signal")
            return

        try:
            targets = self._select_targets(options.get("database"))
        except Exception as exc:
            if not migrator.is_conn_error(exc):
                raise
            self._out(self.stderr, f"default unreachable while selecting targets: {exc}")
            self.stop = Stop("default_unreachable")
            return
        entries = [Entry(t) for t in targets]
        self.entries.extend(entries)
        if not entries:
            self._say("No tenants to migrate.")
            return
        stage = TenantStage.of(entries, bool(options.get("keep_going")), options["max_failures"])
        if self.jobs > 1 and len(entries) > 1 and not self._cli:
            self._say(f"--jobs={self.jobs}: the parallel mode runs only from the command "
                      f"line (§9.6); running sequentially.")
        try:
            if self.jobs > 1 and len(entries) > 1 and self._cli:
                self._run_parallel(stage, opts, min(self.jobs, len(entries)))
            else:
                self._run_sequential(stage, opts)
        finally:
            self.stop = stage.stop
        if not opts.targeted:
            self._recheck_locked()

    def _finish(self, stage: TenantStage, e: Entry) -> None:
        self._log_file(e, migrator.tenant_log_name(e.target.schema, e.target.tenant_id))
        self._line(e)
        stage.done(e)

    def _run_sequential(self, stage: TenantStage, opts: RunOptions) -> None:
        while True:
            if self._drain_requested():
                stage.halt(Stop("signal"))
            if (e := stage.next(1, 1)) is None:         # one in flight
                return
            e.kind = "running"
            e.take(migrator.migrate_tenant(e.target.tenant_id, opts))
            self._finish(stage, e)

    def _run_parallel(self, stage: TenantStage, opts: RunOptions, workers: int) -> None:
        """§9.6. The parent schedules, the workers migrate; the parent never touches the DB
        while the pool lives (its connections are closed before the fork)."""
        connections.close_all()
        buffering = [(s, s.line_buffering) for s in (sys.stdout, sys.stderr)
                     if hasattr(s, "reconfigure")]
        for s, _ in buffering:                  # the parent's result lines stay whole too
            s.reconfigure(line_buffering=True)
        ex = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("fork"),
                                 initializer=_worker_init, initargs=(os.getpid(),))
        running: dict[Future, Entry] = {}
        broken = False
        self._pool_live = True
        try:
            while True:
                if self._drain_requested():
                    stage.halt(Stop("signal"))
                while (e := stage.next(self.jobs, self.per_shard)) is not None:
                    e.kind = "running"
                    running[ex.submit(_work, e.target.tenant_id, opts)] = e
                if not running:
                    return
                done, _ = wait(running, timeout=_POLL_SECS, return_when=FIRST_COMPLETED)
                for f in done:
                    if isinstance(f.exception(), BrokenProcessPool):
                        broken = True
                        continue
                    e = running.pop(f)
                    self._collect(f, e)
                    self._finish(stage, e)
                if broken:
                    break
            # The pool broke: a worker died (OOM killer, our own SIGKILL on a hard stop).
            # Results that made it back still count; the rest was cut off.
            for f, e in list(running.items()):
                if f.done() and f.exception() is None:
                    del running[f]
                    self._collect(f, e)
                    self._finish(stage, e)
            stop = Stop("hard_stop") if self._hard else Stop("broken_pool")
            stage.halt(stop, override=True)
            msg = ("interrupted (hard stop)" if self._hard
                   else "interrupted: a pool worker died (BrokenProcessPool)")
            for e in running.values():
                e.kind, e.reason, e.msg = "interrupted", Reason.HARD_STOP, msg
        finally:
            self._pool_live = False
            if running:                         # broken pool, or an error in this loop
                _kill_workers()
            ex.shutdown(wait=True, cancel_futures=True)
            for s, lb in buffering:
                s.reconfigure(line_buffering=lb)
        cut = list(running.values())
        for e in cut:
            self._line(e)
        self._snapshots(cut)

    @staticmethod
    def _collect(f: Future, e: Entry) -> None:
        """A worker's Result, or a failed entry if migrate_tenant raised in the worker (the
        sequential mode would abort the run; a pool cannot, other tenants are in flight)."""
        exc = f.exception()
        if exc is None:
            e.take(f.result())
        else:
            e.kind, e.reason, e.msg = "failed", Reason.MIGRATION_ERROR, f"{type(exc).__name__}: {exc}"[:500]

    def _select_targets(self, database: str | None) -> list[Target]:
        """§9.3. `-s X`: that tenant, any status. --tenant: NEW/PENDING/ACTIVE/DEACTIVATED
        on the active non-default shards (FAILED and MAINTENANCE are never taken)."""
        if self.tenant is not None:
            t = self.tenant
            return [Target("tenant", t.schema_name, t.shard.alias, t.pk, t.status)]
        qs = (Tenant.objects.select_related("shard")
              .filter(status__in=migrator.PROVISIONABLE + migrator.UPGRADABLE,
                      shard__is_default=False, shard__is_active=True)
              .exclude(schema_name=get_public_schema_name()))
        if database:
            qs = qs.filter(shard__alias=database)
        order = get_tenant_migration_order()
        qs = qs.order_by(*order) if order is not None else qs.order_by("pk")
        return [Target("tenant", t.schema_name, t.shard.alias, t.pk, t.status) for t in qs]

    def _recheck_locked(self) -> None:
        """§9.5: a `locked` skip may have been finished by the lease holder meanwhile."""
        for e in self.entries:
            if not (e.kind == "skipped" and e.reason == Reason.LOCKED):
                continue
            try:
                t = Tenant.objects.select_related("shard").get(pk=e.target.tenant_id)
                if t.status in migrator.UPGRADABLE:
                    if migrator.pending_plan(t) == []:
                        if e.target.status_before in migrator.PROVISIONABLE:
                            self._recheck(e, Reason.DONE_BY_ANOTHER_RUN, "provisioned by another run")
                        else:
                            self._recheck(e, Reason.DONE_BY_ANOTHER_RUN, "done by another run")
                elif t.status == Tenant.Status.PENDING:
                    h = migrator.lease_holder(t.pk)
                    if h is not None and not h.expired:
                        self._recheck(e, Reason.PROVISIONING_ELSEWHERE,
                                      f"provisioning in progress elsewhere ({h.owner})")
            except Exception as exc:                    # the skip stands
                migrator.logger.warning("re-check of %s failed: %s", e.target.schema, exc)

    def _recheck(self, e: Entry, reason: str, msg: str) -> None:
        e.kind, e.reason, e.msg, e.rechecked = "ok", reason, msg, True
        self._line(e, prefix="re-check: ")

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def _out(self, stream: Any, text: str) -> None:
        """Write to the screen, but never let a dead stdout/stderr (Ctrl-C reaches the
        whole process group and kills a `| tee` reader → BrokenPipeError) turn into a
        failed run: the per-target files, summary log and JSON still get written."""
        try:
            stream.write(text)
        except (OSError, ValueError):
            pass

    def _say(self, text: str) -> None:
        if self.verbosity >= 1:
            self._out(self.stdout, self.style.NOTICE(text))

    def _log_file(self, e: Entry, name: str) -> None:
        if self.log_dir is not None and (self.log_dir / name).exists():
            e.log_file = name

    @staticmethod
    def _fmt(e: Entry) -> str:
        line = f"{e.kind:<11} {e.target.schema:<24} {e.target.shard:<10} {e.secs:>8.2f}s  {e.msg or e.reason}"
        if e.invalid_indexes:
            line += f"  INVALID: {', '.join(e.invalid_indexes)}"
        return line

    def _line(self, e: Entry, prefix: str = "") -> None:
        if self.verbosity >= 1:
            self._out(self.stdout, prefix + self._fmt(e))

    def _summary_text(self) -> list[str]:
        counts = Counter(e.kind for e in self.entries)
        lines = ["", "=== migrate_schemas summary ==="]
        lines += [self._fmt(e) for e in self.entries]
        if self.stop is not None:
            lines.append(f"Stop: {self.stop}")
        cut = [e for e in self.entries if e.snapshot is not None]
        if cut:
            lines.append("Interrupted targets, state read from the DB after the stop:")
            lines += [self._fmt_snapshot(e) for e in cut]
        lines.append("  ".join(f"{k}={counts.get(k, 0)}" for k in
                               ("ok", "noop", "failed", "skipped", "not_run", "interrupted")))
        if self.log_dir is not None:
            lines.append(f"Logs: {self.log_dir}")
        return lines

    @staticmethod
    def _fmt_snapshot(e: Entry) -> str:
        s, name = e.snapshot, e.target.schema
        if s.get("error"):
            return f"  {name}: could not read the state ({s['error']})"
        parts = []
        if e.target.tenant_id is not None:
            behind = ("schema missing" if s["behind"] is None and s["status"] != "deleted"
                      else f"{s['behind']} migration(s) behind" if s["behind"] is not None else "")
            parts += [f"status={s['status']}", behind]
        if s["lease_owner"]:
            key = name if e.target.tenant_id is not None else "shared"
            parts += [f"lease held by {s['lease_owner']}",
                      f"once its backend has finished: migration_lease --release {key}"]
        else:
            parts.append("lease free")
        return f"  {name}: " + "; ".join(p for p in parts if p)

    def _error_line(self) -> str:
        counts = Counter(e.kind for e in self.entries)
        bad = ", ".join(f"{counts[k]} {k}" for k in ("failed", "skipped", "not_run", "interrupted")
                        if counts.get(k))
        stop = f"; Stop: {self.stop}" if self.stop is not None else ""
        return f"migrate_schemas: {bad or 'run stopped'}{stop}"

    def _report(self, code: int, options: dict[str, Any]) -> None:
        """Files first, the screen last: a dead stdout must not cost the JSON or the log."""
        text = self._summary_text()
        if options.get("summary_json"):
            self._write_json(Path(options["summary_json"]), code, options)
        if self.log_dir is not None:
            try:
                (self.log_dir / migrator.SUMMARY_LOG).write_text("\n".join(text[1:]) + "\n",
                                                                 encoding="utf-8")
            except OSError as exc:
                self._out(self.stderr, f"could not write {migrator.SUMMARY_LOG}: {exc}")
        if self.verbosity >= 1:
            for line in text:
                self._out(self.stdout, line)

    def _write_json(self, path: Path, code: int, options: dict[str, Any]) -> None:
        stages = [s for s, on in (("shared", self.sync_public), ("tenant", self.sync_tenant)) if on]
        counts = Counter(e.kind for e in self.entries)
        doc = {
            "version": SUMMARY_VERSION,
            "started_at": self.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": socket.gethostname(), "pid": os.getpid(),
            "argv": ["migrate_schemas", *self._argv] if self._argv is not None else None,
            "options": {"stages": stages, "jobs": self.jobs, "jobs_per_shard": self.per_shard,
                        "keep_going": bool(options.get("keep_going")),
                        "max_failures": options.get("max_failures"),
                        "maintenance": bool(options.get("maintenance")),
                        "targeted": any(options.get(k) for k in TARGETED_OPTIONS)},
            "log_dir": str(self.log_dir) if self.log_dir is not None else None,
            "exit_code": code,
            "stop": ({"reason": self.stop.reason, **({"shard": self.stop.shard} if self.stop.shard else {})}
                     if self.stop is not None else None),
            "counts": {k: counts.get(k, 0)
                       for k in ("ok", "noop", "failed", "skipped", "not_run", "interrupted")},
            "results": [{
                "stage": e.target.stage, "schema": e.target.schema, "tenant_id": e.target.tenant_id,
                "shard": e.target.shard, "status_before": e.target.status_before,
                "kind": e.kind, "reason": e.reason, "msg": e.msg, "secs": e.secs,
                "invalid_indexes": list(e.invalid_indexes), "conn_error": e.conn_error,
                "rechecked": e.rechecked, "log_file": e.log_file,
                **({"snapshot": e.snapshot} if e.snapshot is not None else {}),
            } for e in self.entries],
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self._out(self.stderr, f"could not write --summary-json {path}: {exc}")
