"""DB-free tests for tenants.migrator building blocks (deploy/migrations_plan_rev7.md §20.2).

OwnerTests — owner() identity across fork (M2); ClassifyTests — is_conn_error /
is_lock_timeout over real psycopg exception classes, raw and wrapped the way Django wraps
them (`raise <django error> from <driver error>`); StatusGroupsTests, ShardIdleTests,
RunOptionsTests — the small pure pieces of migrate_tenant.
"""
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import psycopg
import psycopg.errors as pg_errors
from django.db import DatabaseError, InterfaceError, OperationalError
from django.test import SimpleTestCase

from tenants import migrator
from tenants.models import Tenant

OWNER_RE = re.compile(r"^(?P<host>[^:]+):(?P<pid>\d+):(?P<start>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$")


class OwnerTests(SimpleTestCase):
    def test_format_host_pid_start(self) -> None:
        m = OWNER_RE.match(migrator.owner())
        self.assertIsNotNone(m, migrator.owner())
        self.assertEqual(m["host"], socket.gethostname())
        self.assertEqual(int(m["pid"]), os.getpid())

    def test_stable_within_process(self) -> None:
        self.assertEqual(migrator.owner(), migrator.owner())

    def test_fork_child_gets_its_own_owner(self) -> None:
        parent = migrator.owner()                   # warm the cache BEFORE forking
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:                                # child: report and leave without cleanup
            try:
                os.close(r)
                os.write(w, migrator.owner().encode())
            finally:
                os._exit(0)
        os.close(w)
        with os.fdopen(r) as f:
            child = f.read()
        os.waitpid(pid, 0)
        self.assertNotEqual(child, parent)
        m = OWNER_RE.match(child)
        self.assertIsNotNone(m, child)
        self.assertEqual(int(m["pid"]), pid)
        self.assertEqual(migrator.owner(), parent)  # the parent's cache is untouched

    def test_start_time_matches_ps_lstart(self) -> None:
        """The start component is the real process start — what a human compares with
        `ps -o lstart= -p <pid>` to rule out a reused pid."""
        if shutil.which("ps") is None:
            self.skipTest("ps not available")
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())],
                             capture_output=True, text=True, check=True,
                             env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}).stdout.strip()
        ps_start = datetime.strptime(out, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
        ours = datetime.strptime(OWNER_RE.match(migrator.owner())["start"],
                                 "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertLessEqual(abs((ours - ps_start).total_seconds()), 1)


def _wrapped(django_cls: type, driver_exc: BaseException) -> BaseException:
    """What Django's DatabaseErrorWrapper produces: the Django class raised FROM the driver one."""
    try:
        try:
            raise driver_exc
        except BaseException as e:
            raise django_cls(*e.args) from e
    except BaseException as outer:
        return outer


class ClassifyTests(SimpleTestCase):
    def test_connection_errors(self) -> None:
        for exc in (
            pg_errors.ConnectionFailure("08006"),
            pg_errors.SqlclientUnableToEstablishSqlconnection("08001"),
            pg_errors.AdminShutdown("57P01"),
            pg_errors.CrashShutdown("57P02"),
            pg_errors.CannotConnectNow("57P03"),
            pg_errors.IdleSessionTimeout("57P05"),                    # server's own timeouts
            pg_errors.IdleInTransactionSessionTimeout("25P03"),       # end the session too
            psycopg.OperationalError("server closed the connection unexpectedly"),  # sqlstate None
            psycopg.InterfaceError("connection already closed"),
        ):
            with self.subTest(raw=type(exc).__name__):
                self.assertTrue(migrator.is_conn_error(exc))
            with self.subTest(wrapped=type(exc).__name__):
                cls = InterfaceError if isinstance(exc, psycopg.InterfaceError) else OperationalError
                self.assertTrue(migrator.is_conn_error(_wrapped(cls, exc)))

    def test_not_connection_errors(self) -> None:
        for exc in (
            _wrapped(OperationalError, pg_errors.LockNotAvailable("lock timeout")),   # 55P03
            _wrapped(OperationalError, pg_errors.QueryCanceled("canceling statement")),  # 57014
            _wrapped(OperationalError, pg_errors.DeadlockDetected("deadlock")),          # 40P01
            _wrapped(OperationalError, pg_errors.DiskFull("disk full")),                 # 53100
            _wrapped(DatabaseError, pg_errors.UndefinedTable("relation x does not exist")),
            ValueError("not a db error"),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(migrator.is_conn_error(exc))

    def test_operational_error_without_any_sqlstate_counts_as_connection(self) -> None:
        """The rule is "sqlstate None", so a Django OperationalError with no driver cause
        (e.g. raised by Django itself on a closed connection) is a connection error."""
        self.assertTrue(migrator.is_conn_error(OperationalError("no driver cause")))

    def test_chain_through_context(self) -> None:
        """A migration error raised while handling a dropped connection still classifies."""
        try:
            try:
                raise _wrapped(OperationalError, pg_errors.AdminShutdown("57P01"))
            except OperationalError:
                raise RuntimeError("migration failed")
        except RuntimeError as e:
            self.assertTrue(migrator.is_conn_error(e))

    def test_cyclic_chain_terminates(self) -> None:
        a, b = RuntimeError("a"), RuntimeError("b")
        a.__context__, b.__context__ = b, a
        self.assertFalse(migrator.is_conn_error(a))
        self.assertFalse(migrator.is_lock_timeout(a))

    def test_lock_timeout(self) -> None:
        self.assertTrue(migrator.is_lock_timeout(pg_errors.LockNotAvailable("x")))
        self.assertTrue(migrator.is_lock_timeout(
            _wrapped(OperationalError, pg_errors.LockNotAvailable("x"))))
        self.assertFalse(migrator.is_lock_timeout(
            _wrapped(OperationalError, pg_errors.QueryCanceled("x"))))
        self.assertFalse(migrator.is_lock_timeout(
            _wrapped(OperationalError, pg_errors.AdminShutdown("x"))))


class StatusGroupsTests(SimpleTestCase):
    """§2.1: the migrator's groups partition Tenant.Status exactly."""

    def test_groups_cover_every_status_once(self) -> None:
        groups = (migrator.PROVISIONABLE, migrator.UPGRADABLE, migrator.OPERATOR_OWNED)
        flat = [s for g in groups for s in g]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(set(flat), set(Tenant.Status.values))


class ShardIdleTests(SimpleTestCase):
    """§6.5: idle = no connection, or transaction_status != ACTIVE (1)."""

    def _idle(self, raw) -> bool:
        with mock.patch.object(migrator, "connections", {"s": SimpleNamespace(connection=raw)}):
            return migrator._shard_idle("s")

    def test_states(self) -> None:
        info = lambda st: SimpleNamespace(info=SimpleNamespace(transaction_status=st))  # noqa: E731
        self.assertTrue(self._idle(None))
        self.assertFalse(self._idle(info(1)))           # ACTIVE: a statement is running
        for st in (0, 2, 3):                            # IDLE, INTRANS, INERROR
            self.assertTrue(self._idle(info(st)))
        self.assertTrue(self._idle(object()))           # broken wrapper without .info


class RunOptionsTests(SimpleTestCase):
    def test_targeted(self) -> None:
        self.assertFalse(migrator.RunOptions().targeted)
        self.assertFalse(migrator.RunOptions(maintenance=True, verbosity=2).targeted)
        for kw in ({"fake": True}, {"fake_initial": True}, {"prune": True},
                   {"run_syncdb": True}, {"app_label": "x"}, {"migration_name": "0001"}):
            with self.subTest(**kw):
                self.assertTrue(migrator.RunOptions(**kw).targeted)

    def test_upstream_options_carry_the_shard(self) -> None:
        o = migrator._upstream_options(migrator.RunOptions(fake=True, app_label="a"), "tenant_2")
        self.assertEqual(o["database"], "tenant_2")
        self.assertEqual((o["fake"], o["app_label"], o["interactive"], o["skip_checks"]),
                         (True, "a", False, True))
        self.assertFalse(o["plan"])
        self.assertFalse(o["check_unapplied"])
