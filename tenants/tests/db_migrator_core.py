"""DB-backed tests for tenants.migrator (deploy/migrations_plan_rev7.md §20.3, steps 1–2
of §21).

No ``test_`` prefix on purpose: the default DB-free discovery must not collect it. Run
against the throwaway servers described in tenants/tests/settings_db.py:

    DJANGO_SETTINGS_MODULE=tenants.tests.settings_db \\
        ../p_env/bin/python manage.py test tenants.tests.db_migrator_core

TestCase wraps every alias in one class-level transaction, so now() is constant within a
class: lease expiry is arranged relative to it (``now() ± interval``), never to wall time.
"""
from datetime import timedelta
from unittest import mock

import psycopg
import psycopg.errors as pg_errors
from django.db import OperationalError, connections
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.migration_executors.base import run_migrations
from django_tenants.utils import get_tenant_base_migrate_command_class

from tenants import migrator
from tenants.models import MigrationLease, Shard, Tenant
from tenants.validators import quote_schema

ALL_DBS = {"default", "tenant_1", "tenant_2"}
OTHER = "other-host:4242:2026-01-01T00:00:00Z"


def _make_shards() -> tuple[Shard, Shard]:
    Shard.objects.create(alias="default", name="public", is_default=True)
    return (Shard.objects.create(alias="tenant_1", name="shard 1"),
            Shard.objects.create(alias="tenant_2", name="shard 2"))


def _put_lease(key: int, owner: str | None, expires_in: str) -> None:
    """Upsert a lease row with until = now() + <expires_in> (a PG interval literal)."""
    with connections["default"].cursor() as cur:
        cur.execute(
            "INSERT INTO tenants_migrationlease (key, owner, since, until)"
            " VALUES (%s, %s, now() - interval '2 hours', now() + %s::interval)"
            " ON CONFLICT (key) DO UPDATE SET owner = EXCLUDED.owner,"
            "   since = EXCLUDED.since, until = EXCLUDED.until",
            [key, owner, expires_in],
        )


def _lease_owner(key: int) -> str | None:
    return MigrationLease.objects.filter(key=key).values_list("owner", flat=True).first()


def _create_schema(alias: str, schema: str) -> None:
    with connections[alias].cursor() as cur:
        cur.execute(f"CREATE SCHEMA {quote_schema(schema)}")


def _migrate_schema(alias: str, schema: str) -> None:
    """Run the real tenant migrations into `schema` on `alias` via upstream run_migrations,
    with the upstream `migrate` parser defaults (what the migrator's _upstream_options will
    build in step 2). Inside TestCase's atomic upstream skips its commit/close."""
    cmd = get_tenant_base_migrate_command_class()()
    options = vars(cmd.create_parser("manage.py", "migrate").parse_args([]))
    options.update(database=alias, interactive=False, skip_checks=True, verbosity=0)
    run_migrations([], options, "standard", schema)


class LeaseTests(TestCase):
    databases = ALL_DBS

    @classmethod
    def setUpTestData(cls) -> None:
        cls.s1, _ = _make_shards()
        cls.t = Tenant.objects.create(schema_name="acme", company_name="Acme", shard=cls.s1)

    def test_acquire_free_creates_row(self) -> None:
        self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))
        lease = MigrationLease.objects.get(key=self.t.pk)
        self.assertEqual(lease.owner, migrator.owner())
        self.assertEqual(lease.until - lease.since, migrator.LEASE_TTL)

    def test_acquire_released_row(self) -> None:
        _put_lease(self.t.pk, None, "1 hour")
        self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))
        self.assertEqual(_lease_owner(self.t.pk), migrator.owner())

    def test_busy_lease_is_skipped(self) -> None:
        _put_lease(self.t.pk, OTHER, "30 minutes")
        msg = migrator.lease_acquire(self.t.pk, "tenant_1")
        self.assertTrue(msg.startswith(f"locked by {OTHER} since "), msg)
        self.assertEqual(_lease_owner(self.t.pk), OTHER)

    def test_own_live_lease_is_not_reentrant(self) -> None:
        self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))
        self.assertTrue(migrator.lease_acquire(self.t.pk, "tenant_1").startswith("locked by "))

    def test_expired_without_session_is_taken_over(self) -> None:
        _put_lease(self.t.pk, OTHER, "-1 second")
        self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))
        self.assertEqual(_lease_owner(self.t.pk), migrator.owner())

    def test_expired_with_active_shard_session_is_skipped_m1(self) -> None:
        """M1: the old run's backend is still working on the shard (even if its client
        died) — the expired lease must not be taken over."""
        _put_lease(self.t.pk, OTHER, "-1 second")
        sd = connections["tenant_1"].settings_dict
        with psycopg.connect(host=sd["HOST"], port=sd["PORT"], user=sd["USER"],
                             password=sd["PASSWORD"], dbname=sd["NAME"],
                             application_name=f"tm:{self.t.pk}") as other:
            other.execute("SELECT 1")               # leaves it 'idle in transaction' = not idle
            busy = migrator.shard_session_active("tenant_1", self.t.pk)
            self.assertIsNotNone(busy)
            self.assertEqual(busy.pid, other.info.backend_pid)
            msg = migrator.lease_acquire(self.t.pk, "tenant_1")
            other.rollback()
        self.assertTrue(msg.startswith("locked (lease expired, backend still active: pid "), msg)
        self.assertEqual(_lease_owner(self.t.pk), OTHER)
        # The session is gone now: the same expired lease is taken over.
        self.assertIsNone(migrator.shard_session_active("tenant_1", self.t.pk))
        self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))

    def test_session_of_another_key_or_shard_does_not_block(self) -> None:
        _put_lease(self.t.pk, OTHER, "-1 second")
        sd = connections["tenant_2"].settings_dict
        with psycopg.connect(host=sd["HOST"], port=sd["PORT"], user=sd["USER"],
                             password=sd["PASSWORD"], dbname=sd["NAME"],
                             application_name=f"tm:{self.t.pk}") as other:
            other.execute("SELECT 1")               # right label, wrong database
            self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))
            other.rollback()

    def test_takeover_two_claimants(self) -> None:
        """Both see the same expired lease; A's upsert wins, and B's ON CONFLICT … WHERE
        until < now() re-checks the row A just wrote (fresh `until`) and matches nothing."""
        _put_lease(self.t.pk, OTHER, "-1 second")
        self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))      # claimant A
        a = migrator.owner()
        with mock.patch.object(migrator, "owner", return_value="b-host:1:2026-01-01T00:00:00Z"):
            msg = migrator.lease_acquire(self.t.pk, "tenant_1")                 # claimant B
        self.assertTrue(msg.startswith(f"locked by {a}"), msg)
        self.assertEqual(_lease_owner(self.t.pk), a)

    def test_insert_race_two_claimants(self) -> None:
        """Both saw a free lease; the loser's ON CONFLICT … WHERE matches nothing."""
        self.assertIsNone(migrator.lease_acquire(self.t.pk, "tenant_1"))      # claimant A
        a = migrator.owner()
        with mock.patch.object(migrator, "owner", return_value="b-host:1:2026-01-01T00:00:00Z"):
            msg = migrator.lease_acquire(self.t.pk, "tenant_1")                 # claimant B
        self.assertTrue(msg.startswith(f"locked by {a}"), msg)
        self.assertEqual(_lease_owner(self.t.pk), a)

    def _orphan(self, alias: str = "tenant_1"):
        """A backend labelled tm:<pk> that is not idle — what a migration whose client lost
        its connection (or was SIGKILLed) leaves running on the shard."""
        sd = connections[alias].settings_dict
        return psycopg.connect(host=sd["HOST"], port=sd["PORT"], user=sd["USER"],
                               password=sd["PASSWORD"], dbname=sd["NAME"],
                               application_name=f"tm:{self.t.pk}")

    def test_free_lease_with_a_running_backend_is_not_taken(self) -> None:
        """No row at all, or a released one (owner NULL): M1 applies to every acquisition."""
        for released in (False, True):
            with self.subTest(released=released):
                if released:
                    _put_lease(self.t.pk, None, "1 hour")
                with self._orphan() as other:
                    other.execute("SELECT 1")                   # 'idle in transaction'
                    msg = migrator.lease_acquire(self.t.pk, "tenant_1")
                    other.rollback()
                self.assertTrue(msg.startswith("locked (lease free, backend still active: pid "), msg)
                self.assertIsNone(_lease_owner(self.t.pk))

    def test_live_lease_with_its_backend_reports_the_owner(self) -> None:
        _put_lease(self.t.pk, OTHER, "1 hour")
        with self._orphan() as other:
            other.execute("SELECT 1")
            msg = migrator.lease_acquire(self.t.pk, "tenant_1")
            other.rollback()
        self.assertTrue(msg.startswith(f"locked by {OTHER} since "), msg)

    def test_unreachable_shard_is_shard_unavailable(self) -> None:
        with mock.patch.object(migrator, "shard_session_active",
                               side_effect=OperationalError("connection refused")), \
             self.assertRaises(migrator.ShardUnavailable):
            migrator.lease_acquire(self.t.pk, "tenant_1")
        self.assertIsNone(_lease_owner(self.t.pk))

    def test_release_only_own(self) -> None:
        _put_lease(self.t.pk, OTHER, "1 hour")
        self.assertFalse(migrator.lease_release(self.t.pk))
        self.assertEqual(_lease_owner(self.t.pk), OTHER)
        _put_lease(self.t.pk, migrator.owner(), "1 hour")
        self.assertTrue(migrator.lease_release(self.t.pk))
        lease = MigrationLease.objects.get(key=self.t.pk)
        self.assertEqual((lease.owner, lease.since, lease.until), (None, None, None))
        self.assertIsNone(migrator.lease_holder(self.t.pk))

    def test_key_zero_uses_default(self) -> None:
        self.assertIsNone(migrator.lease_acquire(0, "default"))
        self.assertEqual(_lease_owner(0), migrator.owner())
        self.assertTrue(migrator.lease_release(0))

    def test_fenced_cas_requires_own_lease_and_expected_status(self) -> None:
        pk = self.t.pk
        # No lease row at all → fence fails.
        self.assertEqual(migrator._fenced_cas(pk, ("new",), status="pending"), 0)
        # Someone else's lease → fence fails.
        _put_lease(pk, OTHER, "1 hour")
        self.assertEqual(migrator._fenced_cas(pk, ("new",), status="pending"), 0)
        # Own lease, even EXPIRED (§4.4: no `until > now()` in the fence) → writes.
        _put_lease(pk, migrator.owner(), "-1 second")
        self.assertEqual(migrator._fenced_cas(pk, ("new",), status="pending",
                                              previous_status="new"), 1)
        t = Tenant.objects.get(pk=pk)
        self.assertEqual((t.status, t.previous_status), ("pending", "new"))
        # Status no longer in `expected` → fails, row untouched.
        self.assertEqual(migrator._fenced_cas(pk, ("new",), status="failed"), 0)
        self.assertEqual(Tenant.objects.get(pk=pk).status, "pending")
        # Deleted tenant → 0 (its lease row survives: no FK).
        Tenant.objects.filter(pk=pk).delete()
        self.assertEqual(migrator._fenced_cas(pk, ("pending",), status="active"), 0)
        self.assertTrue(MigrationLease.objects.filter(key=pk).exists())


class PendingPlanTests(TestCase):
    """B1: a missing schema is MISSING, never "up to date"."""
    databases = ALL_DBS

    @classmethod
    def setUpTestData(cls) -> None:
        cls.s1, cls.s2 = _make_shards()

    def _tenant(self, schema: str, shard: Shard | None = None) -> Tenant:
        return Tenant.objects.create(schema_name=schema, company_name=schema.title(),
                                     shard=shard or self.s1)

    def _assert_conn_back_on_public(self, alias: str) -> None:
        c = connections[alias]
        self.assertEqual((c.schema_name, c.include_public_schema), ("public", True))

    def test_missing_schema_with_public_django_migrations_present(self) -> None:
        c = connections["tenant_1"]
        c.set_schema_to_public()
        MigrationRecorder(c).ensure_schema()            # the trap's precondition
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.django_migrations') IS NOT NULL")
            self.assertTrue(cur.fetchone()[0])
        t = self._tenant("ghost")
        self.assertIs(migrator.pending_plan(t), migrator.MISSING)

    def test_empty_schema_gives_full_plan(self) -> None:
        t = self._tenant("empty_one")
        _create_schema("tenant_1", "empty_one")
        self.assertFalse(migrator.schema_has_relations("tenant_1", "empty_one"))
        plan = migrator.pending_plan(t)
        self.assertIn(("contenttypes", "0001_initial"), plan)
        self.assertIn(("tenants", "0001_initial"), plan)
        self._assert_conn_back_on_public("tenant_1")

    def test_migrated_schema_gives_empty_plan(self) -> None:
        t = self._tenant("done_one")
        _create_schema("tenant_1", "done_one")
        _migrate_schema("tenant_1", "done_one")
        self.assertTrue(migrator.schema_has_relations("tenant_1", "done_one"))
        self.assertEqual(migrator.pending_plan(t), [])
        self._assert_conn_back_on_public("tenant_1")

    def test_plan_is_read_on_the_tenants_own_shard(self) -> None:
        """§5.3: the same schema name migrated on tenant_1 says nothing about tenant_2."""
        _create_schema("tenant_1", "split")
        _migrate_schema("tenant_1", "split")
        t = self._tenant("split", shard=self.s2)
        self.assertIs(migrator.pending_plan(t), migrator.MISSING)
        _create_schema("tenant_2", "split")
        self.assertIn(("contenttypes", "0001_initial"), migrator.pending_plan(t))


class ShardConnectionTests(TestCase):
    """_open_shard / _close_shard inside TestCase's atomic (the reset branch)."""
    databases = ALL_DBS

    def _settings(self, alias: str) -> tuple[str, str]:
        with connections[alias].cursor() as cur:
            cur.execute("SELECT current_setting('lock_timeout'), current_setting('application_name')")
            return cur.fetchone()

    def test_open_sets_and_close_resets(self) -> None:
        base_timeout, _ = self._settings("tenant_1")
        migrator._open_shard("tenant_1", 7)
        self.assertEqual(self._settings("tenant_1"), ("5s", "tm:7"))
        self.assertEqual(self._settings("tenant_2")[0], base_timeout)   # other aliases untouched
        connections["tenant_1"].set_schema("whatever", include_public=False)
        migrator._close_shard("tenant_1")
        timeout, app = self._settings("tenant_1")
        self.assertEqual(timeout, base_timeout)
        self.assertFalse(app.startswith("tm:"))
        self.assertEqual(connections["tenant_1"].schema_name, "public")
        self.assertTrue(connections["tenant_1"].include_public_schema)

    def test_close_outside_atomic_resets_then_closes(self) -> None:
        c = connections["tenant_1"]
        order = []
        # _reset_session mocked: on THIS connection (inside TestCase's transaction) a real
        # reset would roll the test's transaction back; it is tested on its own below.
        with mock.patch.object(c, "in_atomic_block", False), \
             mock.patch.object(migrator, "_reset_session", side_effect=lambda raw: order.append("reset")), \
             mock.patch.object(c, "close", side_effect=lambda: order.append("close")):
            migrator._close_shard("tenant_1")
        self.assertEqual(order, ["reset", "close"])

    def _raw(self, **kw) -> psycopg.Connection:
        sd = connections["tenant_1"].settings_dict
        return psycopg.connect(host=sd["HOST"], port=sd["PORT"], user=sd["USER"],
                               password=sd["PASSWORD"], dbname=sd["NAME"], **kw)

    def test_reset_session_clears_migrator_settings(self) -> None:
        """Review 2026-10-13: behind RDS Proxy a closed client's backend may go back to the
        pool — it must not carry the migrator's lock_timeout / tm:<key> label."""
        for autocommit, in_txn in ((True, False), (False, False), (False, True)):
            with self.subTest(autocommit=autocommit, in_txn=in_txn), self._raw(autocommit=True) as raw:
                default_lt = raw.execute("SHOW lock_timeout").fetchone()[0]
                raw.execute("SELECT set_config('lock_timeout', '5s', false), "
                            "set_config('application_name', 'tm:7', false)")
                raw.autocommit = autocommit
                if in_txn:
                    raw.execute("SELECT 1")                         # INTRANS
                migrator._reset_session(raw)
                raw.autocommit = True
                self.assertEqual(raw.execute("SHOW lock_timeout").fetchone()[0], default_lt)
                self.assertFalse(raw.execute("SHOW application_name").fetchone()[0].startswith("tm:"))

    def test_reset_session_leaves_a_busy_or_broken_session_alone(self) -> None:
        migrator._reset_session(None)                               # no connection
        with self._raw(autocommit=True) as raw:
            raw.close()
            migrator._reset_session(raw)                            # closed: swallowed
        busy = mock.Mock()
        busy.info.transaction_status = psycopg.pq.TransactionStatus.ACTIVE
        migrator._reset_session(busy)
        busy.execute.assert_not_called()
        busy.rollback.assert_not_called()


class FreshDefaultInAtomic(TestCase):
    databases = ALL_DBS

    def test_noop_inside_atomic(self) -> None:
        c = connections["default"]
        c.ensure_connection()
        raw = c.connection
        migrator._fresh()
        self.assertIs(c.connection, raw)
        with c.cursor() as cur:                     # still the same, usable transaction
            cur.execute("SELECT 1")

    def test_closes_outside_atomic(self) -> None:
        c = connections["default"]
        with mock.patch.object(c, "in_atomic_block", False), mock.patch.object(c, "close") as close:
            migrator._fresh()
        close.assert_called_once_with()


class CodeOlderThanDbTests(TestCase):
    databases = ALL_DBS

    def _record(self, app: str, name: str, applied) -> None:
        MigrationRecorder.Migration.objects.using("default").create(app=app, name=name, applied=applied)

    def test_all_known(self) -> None:
        self.assertFalse(migrator.code_older_than_db())

    def test_unknown_applied_after_latest_known(self) -> None:
        self._record("tenants", "9999_from_newer_release", timezone.now() + timedelta(days=1))
        self.assertTrue(migrator.code_older_than_db())

    def test_unknown_applied_before_latest_known_is_a_deleted_old_migration(self) -> None:
        self._record("tenants", "0000_deleted_long_ago", timezone.now() - timedelta(days=3650))
        self.assertFalse(migrator.code_older_than_db())

    def test_unknown_app_is_ignored(self) -> None:
        self._record("ghost_app", "0001_initial", timezone.now() + timedelta(days=1))
        self.assertFalse(migrator.code_older_than_db())


# ---------------------------------------------------------------------------
# Step 2: migrate_tenant / migrate_shared
# ---------------------------------------------------------------------------

def _wrapped(driver_exc: BaseException) -> OperationalError:
    """A Django OperationalError raised FROM a driver error, as DatabaseErrorWrapper does."""
    try:
        try:
            raise driver_exc
        except BaseException as e:
            raise OperationalError(*e.args) from e
    except OperationalError as outer:
        return outer


def _schemas_on(alias: str) -> set[str]:
    with connections[alias].cursor() as cur:
        cur.execute("SELECT nspname FROM pg_namespace")
        return {r[0] for r in cur.fetchall()}


def _session_settings(alias: str) -> tuple[str, str]:
    with connections[alias].cursor() as cur:
        cur.execute("SELECT current_setting('lock_timeout'), current_setting('application_name')")
        return cur.fetchone()


_real_run_migrations = migrator.run_migrations
RUN = migrator.RunOptions(verbosity=0)


class _MigratorCase(TestCase):
    databases = ALL_DBS

    @classmethod
    def setUpTestData(cls) -> None:
        cls.s1, cls.s2 = _make_shards()

    def setUp(self) -> None:
        patcher = mock.patch.object(migrator.resolve_cache, "forget_tenant")
        self.forget = patcher.start()
        self.addCleanup(patcher.stop)

    def tenant(self, schema: str, status: str = "new", shard: Shard | None = None,
               previous_status: str = "new") -> Tenant:
        t = Tenant.objects.create(schema_name=schema, company_name=schema.title(),
                                  shard=shard or self.s1)
        Tenant.objects.filter(pk=t.pk).update(status=status, previous_status=previous_status)
        self.forget.reset_mock()        # create() fired post_save → forget; count only the migrator's
        return Tenant.objects.get(pk=t.pk)

    def reload(self, t: Tenant) -> Tenant:
        return Tenant.objects.get(pk=t.pk)

    def assert_lease_free(self, t: Tenant) -> None:
        self.assertIsNone(migrator.lease_holder(t.pk))

    def assert_forgot_once(self, t: Tenant) -> None:
        self.assertEqual([c.args[0].pk for c in self.forget.call_args_list], [t.pk])


class ProvisionTests(_MigratorCase):
    def test_new_to_active(self) -> None:
        t = self.tenant("prov_ok", shard=self.s2)
        res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason, res.shard), ("ok", "applied", "tenant_2"), res)
        t = self.reload(t)
        self.assertEqual((t.status, t.previous_status, t.last_error), ("active", "pending", ""))
        self.assert_forgot_once(t)
        self.assert_lease_free(t)
        self.assertEqual(migrator.pending_plan(t), [])
        # §5.3: the schema was created on the tenant's shard and nowhere else.
        self.assertIn("prov_ok", _schemas_on("tenant_2"))
        self.assertNotIn("prov_ok", _schemas_on("tenant_1"))
        self.assertNotIn("prov_ok", _schemas_on("default"))
        # §7: the shard connection carries no migrator settings afterwards.
        timeout, app = _session_settings("tenant_2")
        self.assertNotEqual(timeout, "5s")
        self.assertFalse(app.startswith("tm:"))
        self.assertEqual(connections["tenant_2"].schema_name, "public")

    def test_migrations_run_with_lock_timeout_on_the_shard_connection(self) -> None:
        t = self.tenant("prov_lt")
        seen = []

        def spy(args, options, codename, schema):
            seen.append((options["database"], schema, _session_settings(options["database"])))
            return _real_run_migrations(args, options, codename, schema)

        with mock.patch.object(migrator, "run_migrations", side_effect=spy):
            self.assertEqual(migrator.migrate_tenant(t.pk, RUN).kind, "ok")
        self.assertEqual(seen, [("tenant_1", "prov_lt", ("5s", f"tm:{t.pk}"))])

    def test_pending_without_owner_is_resumed(self) -> None:
        t = self.tenant("prov_resume", status="pending")
        with self.assertLogs("tenants.migrate", "INFO") as logs:
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("ok", "resumed"), res)
        self.assertTrue(any("RESUME prov_resume" in line for line in logs.output), logs.output)
        self.assertEqual(self.reload(t).status, "active")

    def test_connection_loss_leaves_pending(self) -> None:
        t = self.tenant("prov_conn")
        with mock.patch.object(migrator, "run_migrations",
                               side_effect=_wrapped(pg_errors.AdminShutdown("terminating"))):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason, res.conn_error), ("failed", "conn_error", "shard"))
        self.assertEqual(self.reload(t).status, "pending")
        self.forget.assert_not_called()
        self.assert_lease_free(t)

    def test_lock_timeout_fails_the_tenant(self) -> None:
        """Q3: 55P03 while provisioning has no special branch → FAILED."""
        t = self.tenant("prov_55p03")
        with mock.patch.object(migrator, "run_migrations",
                               side_effect=_wrapped(pg_errors.LockNotAvailable("lock timeout"))):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason, res.conn_error), ("failed", "lock_timeout", None))
        t = self.reload(t)
        self.assertEqual((t.status, t.previous_status), ("failed", "pending"))
        self.assertIn("LockNotAvailable", t.last_error)
        self.assert_forgot_once(t)
        self.assert_lease_free(t)

    def test_other_error_fails_the_tenant(self) -> None:
        t = self.tenant("prov_err")
        with mock.patch.object(migrator, "run_migrations", side_effect=ValueError("boom")):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("failed", "migration_error"))
        self.assertIn("ValueError: boom", self.reload(t).last_error)
        self.assert_forgot_once(t)

    def test_incomplete_after_run(self) -> None:
        """I4: never ACTIVE while the plan is not empty."""
        t = self.tenant("prov_incomplete")
        with mock.patch.object(migrator, "run_migrations", return_value=None):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("failed", "incomplete_after_run"), res)
        self.assertEqual(self.reload(t).status, "failed")

    def test_keyboard_interrupt_idle_backend_releases_lease(self) -> None:
        t = self.tenant("prov_kbi")
        with mock.patch.object(migrator, "run_migrations", side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual(self.reload(t).status, "pending")     # status not written
        self.forget.assert_not_called()
        self.assert_lease_free(t)

    def test_keyboard_interrupt_active_backend_keeps_lease(self) -> None:
        t = self.tenant("prov_kbi_active")
        with mock.patch.object(migrator, "run_migrations", side_effect=KeyboardInterrupt), \
             mock.patch.object(migrator, "_shard_idle", return_value=False), \
             self.assertRaises(KeyboardInterrupt):
            migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual(self.reload(t).status, "pending")
        self.assertEqual(migrator.lease_holder(t.pk).owner, migrator.owner())

    def test_code_older_than_db_is_skipped(self) -> None:
        t = self.tenant("prov_old")
        with mock.patch.object(migrator, "code_older_than_db", return_value=True):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("skipped", "code_older_than_db"))
        self.assertEqual(self.reload(t).status, "new")
        self.assert_lease_free(t)

    def test_newer_release_during_provisioning_leaves_pending(self) -> None:
        """Review 2026-10-13: an old worker claims, a rollout migrates public meanwhile, the
        old plan completes — the tenant must NOT go ACTIVE on the old schema; it stays
        PENDING (lease freed) so the new code resumes it."""
        t = self.tenant("prov_rollout")
        with mock.patch.object(migrator, "code_older_than_db", side_effect=[False, True]) as older:
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual(older.call_count, 2)                       # at the start AND before ACTIVE
        self.assertEqual((res.kind, res.reason), ("skipped", "code_older_than_db"), res)
        self.assertEqual(self.reload(t).status, "pending")
        self.forget.assert_not_called()                             # PENDING → PENDING: no write
        self.assert_lease_free(t)
        res = migrator.migrate_tenant(t.pk, RUN)                    # the new code resumes it
        self.assertEqual((res.kind, res.reason), ("ok", "resumed"), res)
        self.assertEqual(self.reload(t).status, "active")

    def test_m4_non_empty_schema_fails_schema_exists(self) -> None:
        t = self.tenant("prov_orphan")
        _create_schema("tenant_1", "prov_orphan")
        with connections["tenant_1"].cursor() as cur:
            cur.execute('CREATE TABLE "prov_orphan".stale (id int)')
        res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("failed", "schema_exists"), res)
        t = self.reload(t)
        self.assertEqual((t.status, t.previous_status), ("failed", "new"))
        self.assertTrue(t.last_error.startswith("schema_exists:"))
        self.assert_forgot_once(t)

    def test_m4_accepted_after_admin_reset(self) -> None:
        """FAILED → NEW in admin sets previous_status=failed: the schema is adopted."""
        t = self.tenant("prov_adopt", previous_status="failed")
        _create_schema("tenant_1", "prov_adopt")
        with connections["tenant_1"].cursor() as cur:
            cur.execute('CREATE TABLE "prov_adopt".kept (id int)')
        res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("ok", "applied"), res)
        self.assertEqual(self.reload(t).status, "active")

    def test_m4_empty_schema_goes_the_normal_way(self) -> None:
        t = self.tenant("prov_empty")
        _create_schema("tenant_1", "prov_empty")
        self.assertEqual(migrator.migrate_tenant(t.pk, RUN).kind, "ok")
        self.assertEqual(self.reload(t).status, "active")

    def test_lease_lost_mid_run_writes_no_status(self) -> None:
        t = self.tenant("prov_stolen")

        def run_then_steal(*a, **kw):
            _real_run_migrations(*a, **kw)
            _put_lease(t.pk, OTHER, "1 hour")

        with mock.patch.object(migrator, "run_migrations", side_effect=run_then_steal):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("skipped", "lease_lost"), res)
        self.assertEqual(self.reload(t).status, "pending")
        self.forget.assert_not_called()
        self.assertEqual(_lease_owner(t.pk), OTHER)             # not ours to release

    def test_locked_is_skipped(self) -> None:
        t = self.tenant("prov_locked")
        _put_lease(t.pk, OTHER, "1 hour")
        res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("skipped", "locked"))
        self.assertTrue(res.msg.startswith(f"locked by {OTHER}"))
        self.assertEqual(self.reload(t).status, "new")

    def test_failed_is_skipped(self) -> None:
        t = self.tenant("prov_failed", status="failed")
        res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("skipped", "status_failed"))
        self.assertFalse(MigrationLease.objects.filter(key=t.pk).exists())

    def test_targeted_provisioning_is_refused(self) -> None:
        t = self.tenant("prov_targeted")
        res = migrator.migrate_tenant(t.pk, migrator.RunOptions(fake=True, verbosity=0))
        self.assertEqual(res.kind, "skipped")
        self.assertEqual(self.reload(t).status, "new")

    def test_deleted_default_shard_and_unreachable_default(self) -> None:
        self.assertEqual(migrator.migrate_tenant(10**9, RUN).reason, "tenant_deleted")
        anomaly = Tenant.objects.create(schema_name="on_default", company_name="On Default",
                                        shard=Shard.objects.get(alias="default"))
        res = migrator.migrate_tenant(anomaly.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("failed", "default_shard_anomaly"))
        with mock.patch.object(migrator, "_load",
                               side_effect=_wrapped(pg_errors.AdminShutdown("down"))):
            res = migrator.migrate_tenant(anomaly.pk, RUN)
        self.assertEqual((res.kind, res.conn_error), ("failed", "default"))


class UpgradeTests(_MigratorCase):
    def migrated(self, schema: str, status: str = "active") -> Tenant:
        t = self.tenant(schema, status=status, previous_status="pending")
        _create_schema("tenant_1", schema)
        _migrate_schema("tenant_1", schema)
        return t

    def behind(self, schema: str, status: str = "active") -> Tenant:
        """Schema exists but nothing is applied: a maximal pending plan."""
        t = self.tenant(schema, status=status, previous_status="pending")
        _create_schema("tenant_1", schema)
        return t

    def test_noop_takes_no_lease_and_writes_nothing_to_default(self) -> None:
        t = self.migrated("up_noop")
        with CaptureQueriesContext(connections["default"]) as q:
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("noop", "up_to_date"))
        writes = [x["sql"] for x in q.captured_queries
                  if x["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertFalse(MigrationLease.objects.filter(key=t.pk).exists())

    def test_applies_without_touching_status(self) -> None:
        for status in ("active", "deactivated"):
            with self.subTest(status=status):
                t = self.behind(f"up_apply_{status}", status=status)
                res = migrator.migrate_tenant(t.pk, RUN)
                self.assertEqual((res.kind, res.reason), ("ok", "applied"), res)
                self.assertEqual(self.reload(t).status, status)
                self.assertEqual(migrator.pending_plan(t), [])
                self.assert_lease_free(t)
        self.forget.assert_not_called()

    def test_lost_connection_releases_lease_but_orphan_backend_blocks_the_rerun(self) -> None:
        """Review finding 3: a lost connection → conn_error → the lease is released, while
        the server may still be running the migration. The next run must not start the
        same migration in parallel; once that backend is gone, it proceeds."""
        t = self.behind("up_orphan")
        with mock.patch.object(migrator, "run_migrations",
                               side_effect=OperationalError("server closed the connection unexpectedly")):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.conn_error), ("failed", "shard"), res)
        self.assert_lease_free(t)                                   # released, as designed

        sd = connections["tenant_1"].settings_dict
        with psycopg.connect(host=sd["HOST"], port=sd["PORT"], user=sd["USER"],
                             password=sd["PASSWORD"], dbname=sd["NAME"],
                             application_name=f"tm:{t.pk}") as orphan:
            orphan.execute("SELECT 1")                              # still working server-side
            with mock.patch.object(migrator, "run_migrations") as run:
                res = migrator.migrate_tenant(t.pk, RUN)
            orphan.rollback()
        run.assert_not_called()
        self.assertEqual((res.kind, res.reason), ("skipped", "locked"), res)
        self.assertIn("lease free, backend still active", res.msg)
        self.assert_lease_free(t)

        res = migrator.migrate_tenant(t.pk, RUN)                    # orphan gone → proceeds
        self.assertEqual((res.kind, res.reason), ("ok", "applied"), res)

    def test_failure_writes_no_status(self) -> None:
        t = self.behind("up_fail")
        for exc, reason in ((ValueError("boom"), "migration_error"),
                            (_wrapped(pg_errors.LockNotAvailable("lock timeout")), "lock_timeout")):
            with self.subTest(reason=reason), \
                 mock.patch.object(migrator, "run_migrations", side_effect=exc):
                res = migrator.migrate_tenant(t.pk, RUN)
                self.assertEqual((res.kind, res.reason), ("failed", reason), res)
                t2 = self.reload(t)
                self.assertEqual((t2.status, t2.last_error), ("active", ""))
                self.assert_lease_free(t)
        self.forget.assert_not_called()

    def test_connection_loss_writes_no_status(self) -> None:
        t = self.behind("up_conn")
        with mock.patch.object(migrator, "run_migrations",
                               side_effect=_wrapped(pg_errors.AdminShutdown("terminating"))):
            res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.conn_error), ("failed", "shard"))
        self.assertEqual(self.reload(t).status, "active")

    def test_schema_missing_fails_active_and_deactivated(self) -> None:
        """Q1: the one status write of an upgrade."""
        for status in ("active", "deactivated"):
            with self.subTest(status=status):
                self.forget.reset_mock()
                t = self.tenant(f"up_gone_{status}", status=status, previous_status="pending")
                res = migrator.migrate_tenant(t.pk, RUN)
                self.assertEqual((res.kind, res.reason), ("failed", "schema_missing"), res)
                t = self.reload(t)
                self.assertEqual((t.status, t.previous_status), ("failed", status))
                self.assertIn("missing on tenant_1 during upgrade", t.last_error)
                self.assert_forgot_once(t)
                self.assert_lease_free(t)

    def test_targeted(self) -> None:
        t = self.migrated("up_targeted")
        opts = migrator.RunOptions(verbosity=0, fake=True, app_label="admin", migration_name="zero")
        res = migrator.migrate_tenant(t.pk, opts)
        self.assertEqual((res.kind, res.reason), ("ok", "targeted"), res)
        self.assertRegex(res.msg, r"remaining=[1-9]")
        self.assertEqual(self.reload(t).status, "active")
        self.assert_lease_free(t)


class MaintenanceTests(_MigratorCase):
    """Step-2 part of §20.3 MaintenanceTests: the migrator side only."""

    def test_skipped_without_flag(self) -> None:
        t = self.tenant("mt_skip", status="maintenance", previous_status="active")
        res = migrator.migrate_tenant(t.pk, RUN)
        self.assertEqual((res.kind, res.reason), ("skipped", "status_maintenance"))
        self.assertFalse(MigrationLease.objects.filter(key=t.pk).exists())

    def test_migrates_with_flag_and_never_writes_status(self) -> None:
        t = self.tenant("mt_go", status="maintenance", previous_status="active")
        _create_schema("tenant_1", "mt_go")
        res = migrator.migrate_tenant(t.pk, migrator.RunOptions(verbosity=0, maintenance=True))
        self.assertEqual((res.kind, res.reason), ("ok", "applied"), res)
        t = self.reload(t)
        self.assertEqual((t.status, t.previous_status), ("maintenance", "active"))
        self.assertEqual(migrator.pending_plan(t), [])
        self.forget.assert_not_called()

    def test_missing_schema_with_flag_fails_without_status_write(self) -> None:
        t = self.tenant("mt_gone", status="maintenance", previous_status="active")
        res = migrator.migrate_tenant(t.pk, migrator.RunOptions(verbosity=0, maintenance=True))
        self.assertEqual((res.kind, res.reason), ("failed", "schema_missing"))
        self.assertEqual(self.reload(t).status, "maintenance")
        self.forget.assert_not_called()
        self.assert_lease_free(t)


class SharedStageTests(TestCase):
    """migrate_shared (§6.4) — includes the LeaseTests item "to_regclass → --shared without
    a lease" that needed step 2."""
    databases = ALL_DBS

    def test_runs_under_lease_zero_and_releases_it(self) -> None:
        res = migrator.migrate_shared(RUN)
        self.assertEqual((res.kind, res.reason, res.schema), ("ok", "applied", "public"), res)
        self.assertTrue(MigrationLease.objects.filter(key=0).exists())
        self.assertIsNone(migrator.lease_holder(0))
        timeout, app = _session_settings("default")
        self.assertNotEqual(timeout, "5s")
        self.assertFalse(app.startswith("tm:"))

    def test_busy_fails_immediately(self) -> None:
        _put_lease(0, OTHER, "1 hour")
        with mock.patch.object(migrator, "run_migrations") as run:
            res = migrator.migrate_shared(RUN)
        run.assert_not_called()
        self.assertEqual((res.kind, res.reason), ("failed", "locked"))
        self.assertTrue(res.msg.startswith(f"shared stage busy: held by {OTHER} since "), res.msg)
        self.assertEqual(_lease_owner(0), OTHER)

    def test_fresh_database_runs_without_lease(self) -> None:
        with mock.patch.object(migrator, "_lease_table_exists", return_value=False):
            res = migrator.migrate_shared(RUN)
        self.assertEqual(res.kind, "ok")
        self.assertFalse(MigrationLease.objects.filter(key=0).exists())

    def test_lease_table_probe(self) -> None:
        self.assertTrue(migrator._lease_table_exists())

    def test_failure(self) -> None:
        with mock.patch.object(migrator, "run_migrations", side_effect=ValueError("bad")):
            res = migrator.migrate_shared(RUN)
        self.assertEqual((res.kind, res.reason), ("failed", "migration_error"))
        self.assertIsNone(migrator.lease_holder(0))
