"""DB-backed tests for the migrate_schemas command (deploy/migrations_plan_rev7.md §9, step 3
of §21): target selection (§9.3), the `locked` re-check (§9.5), per-tenant log files on the
CLI path (§9.9) and a few end-to-end runs through the real migrator.

No ``test_`` prefix on purpose (see db_migrator_core.py). Run:

    DJANGO_SETTINGS_MODULE=tenants.tests.settings_db \\
        ../p_env/bin/python manage.py test tenants.tests.db_migrate_schemas

The CLI path is entered by setting ``_cli`` on a command instance and passing it to
call_command: run_from_argv itself would close_all() connections at the end, which breaks
TestCase's wrapping transaction.
"""
import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command, load_command_class
from django.test import override_settings

from tenants import migrator
from tenants.migrator import Result
from tenants.models import Shard, Tenant
from tenants.tests.db_migrator_core import OTHER, _MigratorCase, _put_lease


class _CmdCase(_MigratorCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-db-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = override_settings(TENANT_MIGRATE_LOG_DIR=self.tmp / "logs")
        patcher.enable()
        self.addCleanup(patcher.disable)

    def run_cmd(self, *args, cli: bool = False, **kw) -> tuple[int, str]:
        cmd = load_command_class("tenants", "migrate_schemas")
        cmd._cli = cli
        out = io.StringIO()
        try:
            call_command(cmd, *args, stdout=out, stderr=io.StringIO(), **kw)
            return 0, out.getvalue()
        except CommandError as e:
            return e.returncode, out.getvalue()

    def run_dir(self) -> Path:
        (run,) = list((self.tmp / "logs").iterdir())
        return run


class TargetSelectionTests(_CmdCase):
    def select(self, database=None) -> list[str]:
        cmd = load_command_class("tenants", "migrate_schemas")
        cmd.tenant = None
        return [t.schema for t in cmd._select_targets(database)]

    def test_statuses_shards_and_order(self) -> None:
        off = Shard.objects.create(alias="tenant_off", name="off", is_active=False)
        for schema, status, shard in [("s_new", "new", self.s1), ("s_pend", "pending", self.s2),
                                      ("s_act", "active", self.s1), ("s_deact", "deactivated", self.s2),
                                      ("s_fail", "failed", self.s1), ("s_maint", "maintenance", self.s1),
                                      ("s_off", "active", off)]:
            self.tenant(schema, status=status, shard=shard)
        self.assertEqual(self.select(), ["s_new", "s_pend", "s_act", "s_deact"])
        self.assertEqual(self.select("tenant_2"), ["s_pend", "s_deact"])
        self.assertEqual(self.select("default"), [])


class CommandRunTests(_CmdCase):
    def test_single_tenant_provisioning_via_call_command_writes_no_files(self) -> None:
        t = self.tenant("cmd_prov")
        code, out = self.run_cmd("-s", "cmd_prov")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.reload(t).status, "active")
        self.assertFalse((self.tmp / "logs").exists())

    def test_full_cli_run_writes_per_target_logs(self) -> None:
        self.tenant("cli_a")
        self.tenant("cli_b", shard=self.s2)
        summary = self.tmp / "s.json"
        code, out = self.run_cmd(f"--summary-json={summary}", cli=True)
        self.assertEqual(code, 0, out)
        run = self.run_dir()
        self.assertEqual(sorted(p.name for p in run.iterdir()),
                         ["_shared.log", "_summary.log", "cli_a.log", "cli_b.log"])
        text = (run / "cli_a.log").read_text()
        self.assertTrue(text.startswith("schema: cli_a\nshard: tenant_1\nstatus: new\n"), text[:200])
        self.assertIn(f"lease owner: {migrator.owner()}", text)
        # Upstream wraps Django's OutputWrapper in its own, so " OK" lands on its own line —
        # the same on screen; the file mirrors it.
        self.assertIn("\n  Applying users.0001_initial...\n OK\n", text)
        self.assertNotIn("[standard:", text)
        self.assertRegex(text.rstrip().splitlines()[-1], r"^result: ok applied \d+\.\d\ds — provisioned; invalid indexes: none$")
        self.assertIn(f"Logs: {run}", out)
        doc = json.loads(summary.read_text())
        self.assertEqual([(r["schema"], r["kind"], r["log_file"]) for r in doc["results"]],
                         [("public", "ok", "_shared.log"), ("cli_a", "ok", "cli_a.log"),
                          ("cli_b", "ok", "cli_b.log")])
        self.assertEqual(doc["log_dir"], str(run))

    def test_schema_named_summary_keeps_its_own_log(self) -> None:
        self.tenant("summary")
        code, out = self.run_cmd("-s", "summary", cli=True)
        self.assertEqual(code, 0, out)
        run = self.run_dir()
        self.assertTrue((run / "summary.log").read_text().startswith("schema: summary\n"))
        self.assertIn("=== migrate_schemas summary ===", out)
        self.assertNotIn("schema:", (run / "_summary.log").read_text())     # the run's, untouched

    def test_unsafe_legacy_schema_name_cannot_escape_the_run_folder(self) -> None:
        """The model field accepts '../evil' (only Tenant.clean() / the API validate)."""
        t = self.tenant("../evil")
        self.run_cmd("-s", "../evil", cli=True)
        run = self.run_dir()
        self.assertTrue((run / f"_tenant_{t.pk}.log").read_text().startswith("schema: ../evil\n"))
        self.assertEqual([p for p in (self.tmp / "logs").iterdir() if p.is_file()], [])
        self.assertFalse((self.tmp / "evil.log").exists())

    def test_failure_traceback_goes_to_the_file(self) -> None:
        self.tenant("cli_fail")
        with mock.patch.object(migrator, "run_migrations", side_effect=ValueError("boom 42")):
            code, out = self.run_cmd("-s", "cli_fail", cli=True)
        self.assertEqual(code, 1)
        text = (self.run_dir() / "cli_fail.log").read_text()
        self.assertIn("Traceback (most recent call last):", text)
        self.assertIn("ValueError: boom 42", text)
        self.assertIn("result: failed migration_error", text)
        self.assertNotIn("Traceback", out)              # the screen gets the Result line only

    def test_failed_tenant_by_name_is_skipped_exit_1(self) -> None:
        self.tenant("cli_failed", status="failed")
        code, out = self.run_cmd("-s", "cli_failed")
        self.assertEqual(code, 1)
        self.assertIn("status_failed", out)

    def test_maintenance_needs_the_flag(self) -> None:
        self.tenant("cli_mt", status="maintenance", previous_status="active")
        self.assertEqual(self.run_cmd("-s", "cli_mt")[0], 1)
        with mock.patch.object(migrator, "run_migrations") as run:
            code, out = self.run_cmd("-s", "cli_mt", "--maintenance")
        self.assertEqual((code, run.call_count), (1, 0), out)     # no schema → failed, unwritten
        self.assertIn('schema "cli_mt" missing on tenant_1 during upgrade', out)
        self.assertEqual(Tenant.objects.get(schema_name="cli_mt").status, "maintenance")

    def test_targeted_refused_for_provisioning(self) -> None:
        self.tenant("cli_tgt")
        with self.assertRaisesMessage(CommandError, "not allowed for a new tenant"):
            call_command("migrate_schemas", "-s", "cli_tgt", "--fake", stdout=io.StringIO())

    def test_database_must_match(self) -> None:
        self.tenant("cli_db")
        with self.assertRaisesMessage(CommandError, "does not match"):
            call_command("migrate_schemas", "-s", "cli_db", "--database=tenant_2",
                         stdout=io.StringIO())

    def test_tenant_stage_database_must_be_a_live_tenant_shard(self) -> None:
        """Review 2026-10-13: `--tenant --database X` used to print "No tenants to migrate"
        and exit 0 for the default shard, an inactive shard or an unregistered alias."""
        Shard.objects.filter(alias="tenant_2").update(is_active=False)
        for db, hint in (("default", "the default shard holds only public"),
                         ("tenant_2", "shard is inactive and --tenant skips inactive shards")):
            with self.subTest(db=db), self.assertRaisesMessage(CommandError, hint):
                call_command("migrate_schemas", "--tenant", f"--database={db}", stdout=io.StringIO())
        Shard.objects.filter(alias="tenant_2").delete()
        with self.assertRaisesMessage(CommandError, "no Shard is registered for this alias"):
            call_command("migrate_schemas", "--tenant", "--database=tenant_2", stdout=io.StringIO())
        code, out = self.run_cmd("--tenant", "--database=tenant_1")      # a live shard: fine
        self.assertEqual(code, 0, out)

    def test_unknown_schema(self) -> None:
        with self.assertRaisesMessage(CommandError, "not found"):
            call_command("migrate_schemas", "-s", "nobody", stdout=io.StringIO())


class RecheckLockedTests(_CmdCase):
    def test_locked_then_done_by_another_run(self) -> None:
        t = self.tenant("rc_done")
        self.assertEqual(migrator.migrate_tenant(t.pk, migrator.RunOptions(verbosity=0)).kind, "ok")
        locked = Result("skipped", "rc_done", "tenant_1", "locked", f"locked by {OTHER}")
        summary = self.tmp / "s.json"
        with mock.patch.object(migrator, "migrate_tenant", return_value=locked):
            code, out = self.run_cmd("-s", "rc_done", f"--summary-json={summary}")
        self.assertEqual(code, 0, out)
        (r,) = json.loads(summary.read_text())["results"]
        self.assertEqual((r["kind"], r["reason"], r["rechecked"]), ("ok", "done_by_another_run", True))

    def test_pending_with_live_holder_is_provisioning_elsewhere(self) -> None:
        t = self.tenant("rc_pend", status="pending")
        _put_lease(t.pk, OTHER, "1 hour")
        code, out = self.run_cmd("-s", "rc_pend")
        self.assertEqual(code, 0, out)
        self.assertIn("provisioning in progress elsewhere", out)

    def test_still_behind_stays_skipped(self) -> None:
        t = self.tenant("rc_behind", status="active", previous_status="active")
        from tenants.tests.db_migrator_core import _create_schema
        _create_schema("tenant_1", "rc_behind")         # empty schema → full plan, never noop
        _put_lease(t.pk, OTHER, "1 hour")
        code, out = self.run_cmd("-s", "rc_behind")
        self.assertEqual(code, 1)
        self.assertIn("locked by", out)
        self.assertNotIn("re-check", out)

    def test_targeted_run_is_not_rechecked(self) -> None:
        t = self.tenant("rc_tgt", status="active", previous_status="active")
        locked = Result("skipped", "rc_tgt", "tenant_1", "locked", "locked")
        with mock.patch.object(migrator, "migrate_tenant", return_value=locked), \
             mock.patch.object(migrator, "pending_plan", return_value=[]) as plan:
            code, _ = self.run_cmd("-s", "rc_tgt", "--fake")
        self.assertEqual(code, 1)
        plan.assert_not_called()


class SnapshotTests(_CmdCase):
    """migrator.snapshot() (§9.7): what the summary shows for a target cut off by a hard stop."""

    def test_pending_tenant_with_kept_lease(self) -> None:
        from tenants.tests.db_migrator_core import _create_schema
        t = self.tenant("snap_p", status="pending")
        _create_schema("tenant_1", "snap_p")            # empty schema → the full plan is behind
        _put_lease(t.pk, OTHER, "1 hour")
        snap = migrator.snapshot(t.pk)
        self.assertEqual((snap["status"], snap["lease_owner"]), ("pending", OTHER))
        self.assertGreater(snap["behind"], 0)
        self.assertNotIn("error", snap)

    def test_missing_schema_free_lease_deleted_and_shared(self) -> None:
        t = self.tenant("snap_m", status="active", previous_status="active")
        self.assertEqual(migrator.snapshot(t.pk), {"status": "active", "behind": None, "lease_owner": None})
        self.assertEqual(migrator.snapshot(10 ** 9)["status"], "deleted")
        _put_lease(0, OTHER, "1 hour")
        self.assertEqual(migrator.snapshot(0), {"status": None, "behind": None, "lease_owner": OTHER})
