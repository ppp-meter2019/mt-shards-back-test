"""DB-backed tests for migration_lease and the read-only reconcile_tenants
(deploy/migrations_plan_rev7.md §10, §11, §20.3; step 5 of §21).

No ``test_`` prefix on purpose (see db_migrator_core.py). Run:

    DJANGO_SETTINGS_MODULE=tenants.tests.settings_db \\
        ../p_env/bin/python manage.py test tenants.tests.db_lease_reconcile
"""
import io
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from unittest import mock

import psycopg
from django.core.management import CommandError, call_command
from django.db import OperationalError, connections
from django.test.utils import CaptureQueriesContext

from tenants import migrator
from tenants.models import Tenant
from tenants.tests.db_migrator_core import (
    ALL_DBS, OTHER, _MigratorCase, _create_schema, _lease_owner, _put_lease,
)


def _cmd(name: str, *args) -> tuple[int, str]:
    out = io.StringIO()
    try:
        call_command(name, *args, stdout=out, stderr=io.StringIO())
        return 0, out.getvalue()
    except CommandError as e:
        return e.returncode, out.getvalue() + f"\n!! {e}"


@contextmanager
def _active_backend(alias: str, key: int):
    """A second session labelled tm:<key> left 'idle in transaction' = not idle (M1)."""
    sd = connections[alias].settings_dict
    with psycopg.connect(host=sd["HOST"], port=sd["PORT"], user=sd["USER"],
                         password=sd["PASSWORD"], dbname=sd["NAME"],
                         application_name=f"tm:{key}") as other:
        other.execute("SELECT 1")
        try:
            yield other.info.backend_pid
        finally:
            other.rollback()


class MigrationLeaseCmdTests(_MigratorCase):
    def test_list_empty(self) -> None:
        self.assertEqual(_cmd("migration_lease", "--list"), (0, "No leases held.\n"))

    def test_list_rows(self) -> None:
        t = self.tenant("ml_a", status="active")
        _put_lease(t.pk, OTHER, "30 minutes")
        _put_lease(0, OTHER, "-1 minute")
        _put_lease(987654, OTHER, "-1 minute")              # its tenant is gone
        _put_lease(424242, None, "1 hour")                   # free: not listed
        with _active_backend("tenant_1", t.pk) as pid:
            code, out = _cmd("migration_lease", "--list")
        self.assertEqual(code, 0)
        rows = {line.split()[0]: line for line in out.splitlines()[1:]}
        self.assertEqual(sorted(rows), sorted(["0", str(t.pk), "987654"]))
        self.assertIn("shared", rows["0"])
        self.assertIn("EXPIRED", rows["0"])
        self.assertIn("ml_a", rows[str(t.pk)])
        self.assertIn(" live ", rows[str(t.pk)])
        self.assertIn(f"pid {pid} on tenant_1", rows[str(t.pk)])
        self.assertIn("tenant deleted (key=987654)", rows["987654"])
        self.assertTrue(rows["987654"].rstrip().endswith("none"))

    def test_release(self) -> None:
        t = self.tenant("ml_r", status="active")
        _put_lease(t.pk, OTHER, "-1 minute")
        code, out = _cmd("migration_lease", "--release", "ml_r")
        self.assertEqual(code, 0, out)
        self.assertIn(f"Released lease of ml_r (key {t.pk}): owner {OTHER}", out)
        self.assertIn("(EXPIRED)", out)
        self.assertIsNone(_lease_owner(t.pk))

    def test_release_refused_while_backend_active_then_force(self) -> None:
        t = self.tenant("ml_busy", status="pending")
        _put_lease(t.pk, OTHER, "1 hour")
        with _active_backend("tenant_1", t.pk) as pid:
            code, out = _cmd("migration_lease", "--release", "ml_busy")
            self.assertEqual(code, 1)
            self.assertIn(f"still running server-side (pid {pid} on tenant_1", out)
            self.assertEqual(_lease_owner(t.pk), OTHER)
            code, out = _cmd("migration_lease", "--release", "ml_busy", "--force")
        self.assertEqual(code, 0, out)
        self.assertIn("[--force]", out)
        self.assertIsNone(_lease_owner(t.pk))

    def test_live_lease_needs_force(self) -> None:
        """No backend visible, but the lease has not expired: its owner may be alive between
        two statements (idle in pg_stat_activity), so a plain --release refuses."""
        t = self.tenant("ml_live", status="pending")
        _put_lease(t.pk, OTHER, "45 minutes")
        code, out = _cmd("migration_lease", "--release", "ml_live")
        self.assertEqual(code, 1)
        self.assertIn("is live until", out)
        self.assertIn("--force", out)
        self.assertEqual(_lease_owner(t.pk), OTHER)
        code, out = _cmd("migration_lease", "--release", "ml_live", "--force")
        self.assertEqual(code, 0, out)
        self.assertIsNone(_lease_owner(t.pk))

    def test_not_held(self) -> None:
        self.tenant("ml_free")
        code, out = _cmd("migration_lease", "--release", "ml_free")
        self.assertEqual(code, 0)
        self.assertIn("is not held; nothing to release", out)

    def test_shared_and_key_forms(self) -> None:
        _put_lease(0, OTHER, "-1 minute")
        self.assertEqual(_cmd("migration_lease", "--release", "shared")[0], 0)
        self.assertIsNone(_lease_owner(0))
        _put_lease(987654, OTHER, "-1 minute")
        code, out = _cmd("migration_lease", "--release", "key=987654")
        self.assertEqual(code, 0, out)
        self.assertIn("key=987654 (tenant deleted)", out)
        self.assertIsNone(_lease_owner(987654))

    def test_deleted_tenant_backend_is_looked_for_on_every_shard(self) -> None:
        _put_lease(987654, OTHER, "1 hour")
        with _active_backend("tenant_2", 987654):
            code, out = _cmd("migration_lease", "--release", "key=987654")
        self.assertEqual(code, 1)
        self.assertIn("on tenant_2", out)

    def test_bad_targets(self) -> None:
        t = self.tenant("shared")
        self.assertIn("ambiguous", _cmd("migration_lease", "--release", "shared")[1])
        self.assertIn(f"key={t.pk} for the tenant", _cmd("migration_lease", "--release", "shared")[1])
        self.assertIn("not found", _cmd("migration_lease", "--release", "nobody")[1])
        self.assertIn("integer", _cmd("migration_lease", "--release", "key=x")[1])
        self.assertIn("only with --release", _cmd("migration_lease", "--list", "--force")[1])

    def test_release_is_fenced_on_the_owner_shown(self) -> None:
        """Between the check and the UPDATE a new run re-took the lease: not touched."""
        t = self.tenant("ml_fence", status="active")
        _put_lease(t.pk, OTHER, "1 hour")
        stale = migrator.LeaseHolder("dead-host:1:2026-01-01T00:00:00Z",
                                     datetime(2026, 1, 1, tzinfo=timezone.utc),
                                     datetime(2026, 1, 1, 1, tzinfo=timezone.utc), True)
        with mock.patch.object(migrator, "lease_holder", side_effect=[stale, migrator.lease_holder(t.pk)]):
            code, out = _cmd("migration_lease", "--release", "ml_fence")
        self.assertEqual(code, 1)
        self.assertIn("changed hands meanwhile", out)
        self.assertEqual(_lease_owner(t.pk), OTHER)


class ReconcileTests(_MigratorCase):
    def active(self, schema: str, shard=None) -> Tenant:
        t = self.tenant(schema, shard=shard)
        res = migrator.migrate_tenant(t.pk, migrator.RunOptions(verbosity=0))
        self.assertEqual(res.kind, "ok", res)
        return self.reload(t)

    def test_check_ok_and_writes_nothing(self) -> None:
        self.active("rc_ok")
        self.tenant("rc_new")                                # not checked
        self.tenant("rc_deact", status="deactivated")        # no schema, not checked
        self.tenant("rc_maint", status="maintenance")        # no schema, not checked
        self.tenant("rc_failed", status="failed")
        with ExitStack() as stack:
            caps = [stack.enter_context(CaptureQueriesContext(connections[a])) for a in sorted(ALL_DBS)]
            code, out = _cmd("reconcile_tenants", "--check")
        self.assertEqual(code, 0, out)
        self.assertIn("checked_active=1 behind=0 missing=0 pending_stuck=0 unreachable_shards=[]", out)
        writes = [q["sql"] for c in caps for q in c.captured_queries
                  if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"))]
        self.assertEqual(writes, [])

    def test_check_missing_and_behind_fail(self) -> None:
        self.tenant("rc_missing", status="active", previous_status="active")
        self.tenant("rc_behind", status="active", previous_status="active", shard=self.s2)
        _create_schema("tenant_2", "rc_behind")               # empty → full plan behind
        code, out = _cmd("reconcile_tenants", "--check")
        self.assertEqual(code, 1)
        self.assertIn("FAIL rc_missing (tenant_1): ACTIVE but its schema is missing", out)
        self.assertRegex(out, r"FAIL rc_behind \(tenant_2\): \d+ migration\(s\) behind — run "
                              r"`migrate_schemas -s rc_behind`")
        self.assertIn("checked_active=2 behind=1 missing=1", out)

    def test_inactive_shard_is_checked_with_a_hint(self) -> None:
        t = self.tenant("rc_off", status="active", previous_status="active")
        _create_schema("tenant_1", "rc_off")
        self.s1.is_active = False
        self.s1.save()
        code, out = _cmd("reconcile_tenants", "--check")
        self.assertEqual(code, 1)
        self.assertIn("shard tenant_1 is inactive, so `--tenant` skips it", out)

    def test_stuck_pending_is_a_warning_only(self) -> None:
        self.tenant("rc_stuck", status="pending")
        t = self.tenant("rc_busy", status="pending")
        _put_lease(t.pk, OTHER, "1 hour")
        code, out = _cmd("reconcile_tenants", "--check")
        self.assertEqual(code, 0, out)
        self.assertIn("WARN stuck PENDING rc_stuck (tenant_1): stuck (no lease, no active backend)", out)
        self.assertNotIn("rc_busy", out)
        self.assertIn("pending_stuck=1", out)

    def test_unreachable_shard(self) -> None:
        self.active("rc_s1")
        self.active("rc_s2", shard=self.s2)
        real = migrator.behind

        def behind(t):
            if t.shard.alias == "tenant_2":
                raise OperationalError("connection refused")
            return real(t)
        with mock.patch.object(migrator, "behind", side_effect=behind):
            code, out = _cmd("reconcile_tenants", "--check")
        self.assertEqual(code, 1)
        self.assertIn("unreachable_shards=['tenant_2']", out)

    def test_report_rows_and_plan(self) -> None:
        self.active("rp_ok")
        self.tenant("rp_new", shard=self.s2)
        _create_schema("tenant_2", "rp_new")
        failed = self.tenant("rp_failed", status="failed")
        Tenant.objects.filter(pk=failed.pk).update(last_error="Traceback…\nValueError: boom")
        p = self.tenant("rp_pend", status="pending")
        _put_lease(p.pk, OTHER, "-1 minute")
        code, out = _cmd("reconcile_tenants", "--report", "--plan")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("public "), lines[:3])
        row = {l.split()[0]: l for l in lines if l and not l.startswith(" ")}
        self.assertRegex(row["rp_ok"], r"active\s+0\s+-\s+-$")
        self.assertIn("migrate_schemas -s rp_new", row["rp_new"])
        self.assertIn("   - users.0001_initial", out)            # --plan names (rp_new's)
        self.assertIn("admin: FAILED → NEW", row["rp_failed"])
        self.assertIn("last_error: ValueError: boom", out)
        self.assertIn(f"{OTHER} since", row["rp_pend"])
        self.assertIn("EXPIRED", row["rp_pend"])
        self.assertIn("migration_lease --release rp_pend, then rerun", row["rp_pend"])
        self.assertIn("PENDING stuck (lease expired, no active backend)", out)

    def test_report_filters(self) -> None:
        self.tenant("rf_a")
        self.tenant("rf_b", status="pending")
        code, out = _cmd("reconcile_tenants", "--report", "-s", "rf_a")
        self.assertEqual(code, 0)
        self.assertIn("rf_a", out)
        self.assertNotIn("rf_b", out)
        self.assertNotIn("public", out)
        code, out = _cmd("reconcile_tenants", "--report", "--only-pending")
        self.assertIn("rf_b", out)
        self.assertNotIn("rf_a ", out)
        self.assertIn("not found", _cmd("reconcile_tenants", "--report", "-s", "nobody")[1])

    def test_flags(self) -> None:
        self.assertIn("only with --report", _cmd("reconcile_tenants", "--check", "--plan")[1])
        with self.assertRaises(CommandError):
            call_command("reconcile_tenants", stdout=io.StringIO())     # neither mode


class HiddenSessionStateTests(_MigratorCase):
    """pg_stat_activity seen by a role WITHOUT pg_read_all_stats: another role's session
    has state = NULL. The M1 query must count it as busy (review 2026-10-12)."""

    def test_other_roles_session_counts_as_busy(self) -> None:
        sd = connections["tenant_1"].settings_dict
        base = dict(host=sd["HOST"], port=sd["PORT"], dbname=sd["NAME"])
        with psycopg.connect(**base, user=sd["USER"], password=sd["PASSWORD"], autocommit=True) as su:
            for role in ("tm_t_checker", "tm_t_owner"):
                su.execute(f"DROP ROLE IF EXISTS {role}")
                su.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'pw'")
            try:
                with psycopg.connect(**base, user="tm_t_owner", password="pw",
                                     application_name="tm:777") as owner, \
                     psycopg.connect(**base, user="tm_t_checker", password="pw",
                                     autocommit=True) as checker:
                    owner.execute("SELECT 1")                       # idle in transaction
                    state = checker.execute(
                        "SELECT state FROM pg_stat_activity WHERE application_name = 'tm:777'"
                    ).fetchone()
                    self.assertEqual(state, (None,))                # hidden from this role
                    old_sql = migrator._SESSION_ACTIVE_SQL.replace(
                        "coalesce(state, 'unknown') <> 'idle'", "state <> 'idle'")
                    self.assertIsNone(checker.execute(old_sql, ["tm:777"]).fetchone())
                    row = checker.execute(migrator._SESSION_ACTIVE_SQL, ["tm:777"]).fetchone()
                    self.assertIsNotNone(row)                        # now counted as busy
                    self.assertEqual(row[0], owner.info.backend_pid)
                    owner.rollback()
            finally:
                for role in ("tm_t_checker", "tm_t_owner"):
                    su.execute(f"DROP ROLE IF EXISTS {role}")


class ServerErrorClassificationTests(_MigratorCase):
    """is_conn_error against what a real server and psycopg 3 actually raise
    (review 2026-10-13): these must leave a provisioning tenant PENDING, not FAILED."""

    def _base(self) -> dict:
        sd = connections["tenant_1"].settings_dict
        return dict(host=sd["HOST"], port=sd["PORT"], dbname=sd["NAME"])

    def test_too_many_connections_at_connect_has_no_sqlstate(self) -> None:
        sd = connections["tenant_1"].settings_dict
        with psycopg.connect(**self._base(), user=sd["USER"], password=sd["PASSWORD"],
                             autocommit=True) as su:
            su.execute("DROP ROLE IF EXISTS tm_t_lim")
            su.execute("CREATE ROLE tm_t_lim LOGIN PASSWORD 'pw' CONNECTION LIMIT 1")
            try:
                with psycopg.connect(**self._base(), user="tm_t_lim", password="pw"):
                    with self.assertRaises(psycopg.OperationalError) as ctx:
                        psycopg.connect(**self._base(), user="tm_t_lim", password="pw")
                self.assertIn("too many connections", str(ctx.exception))
                self.assertIsNone(ctx.exception.sqlstate)            # 53300 is not exposed
                self.assertTrue(migrator.is_conn_error(ctx.exception))
                self.assertTrue(migrator.is_conn_error(OperationalError(str(ctx.exception))))
            finally:
                su.execute("DROP ROLE IF EXISTS tm_t_lim")

    def test_server_side_session_timeouts(self) -> None:
        import time
        sd = connections["tenant_1"].settings_dict
        for setting, autocommit, code in (("idle_session_timeout", True, "57P05"),
                                          ("idle_in_transaction_session_timeout", False, "25P03")):
            with self.subTest(code=code):
                c = psycopg.connect(**self._base(), user=sd["USER"], password=sd["PASSWORD"],
                                    autocommit=autocommit)
                try:
                    c.execute(f"SET {setting} = '200ms'")
                    if not autocommit:
                        c.execute("SELECT 1")                      # open transaction, then idle
                    time.sleep(0.8)
                    with self.assertRaises(psycopg.Error) as ctx:
                        c.execute("SELECT 1")
                finally:
                    c.close()
                if ctx.exception.sqlstate is not None:             # the FATAL arrived with its code
                    self.assertEqual(ctx.exception.sqlstate, code)
                self.assertTrue(migrator.is_conn_error(ctx.exception), repr(ctx.exception))
