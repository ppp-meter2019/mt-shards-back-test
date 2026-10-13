"""End-to-end migrator tests on real connections (deploy/migrations_plan_rev7.md §20.4).
TransactionTestCase: no wrapping transaction, so upstream commits and closes for real and
forked pool workers see the committed tenants. ParallelE2E (step 4 of §21); ProvisionE2E,
ReentryE2E, LockTimeoutE2E (step 6) need the canary app and skip without it;
CeleryConnectionHygieneTests (step 7).

No ``test_`` prefix on purpose (see db_migrator_core.py). Run with the canary:

    DJANGO_SETTINGS_MODULE=tenants.tests.settings_db_canary \\
        ../p_env/bin/python manage.py test tenants.tests.db_migrator_e2e
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import psycopg
from django.apps import apps

from django.conf import settings
from django.core.management import CommandError, call_command, load_command_class
from django.db import connections
from django.test import TransactionTestCase, override_settings

from tenants import migrator
from tenants.models import MigrationLease, Tenant
from tenants.tests.db_migrator_core import ALL_DBS, _make_shards
from tenants.validators import quote_schema


def _connect(alias: str, **kw) -> psycopg.Connection:
    sd = connections[alias].settings_dict
    return psycopg.connect(host=sd["HOST"], port=sd["PORT"], user=sd["USER"],
                           password=sd["PASSWORD"], dbname=sd["NAME"], **kw)


class _E2ECase(TransactionTestCase):
    databases = ALL_DBS
    schemas: list[tuple[str, str]] = []                 # (schema, alias) to drop afterwards

    def setUp(self) -> None:
        self.s1, self.s2 = _make_shards()
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        forget = mock.patch.object(migrator.resolve_cache, "forget_tenant")
        forget.start()
        self.addCleanup(forget.stop)
        self.addCleanup(self._drop_schemas)

    def _drop_schemas(self) -> None:
        for schema, alias in self.schemas:
            with connections[alias].cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {quote_schema(schema)} CASCADE")

    def run_cmd(self, *args) -> tuple[int, dict]:
        summary = self.tmp / "s.json"
        try:
            call_command("migrate_schemas", *args, f"--summary-json={summary}",
                         stdout=io.StringIO(), stderr=io.StringIO())
            code = 0
        except CommandError as e:
            code = e.returncode
        return code, json.loads(summary.read_text())

    def scalar(self, alias: str, sql: str):
        with connections[alias].cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]


_CANARY = unittest.skipUnless(apps.is_installed("tenants.tests.canary"),
                              "needs DJANGO_SETTINGS_MODULE=tenants.tests.settings_db_canary")


@_CANARY
class ProvisionE2E(_E2ECase):
    """NEW → ACTIVE through 0001–0004 of the canary, outside any test transaction: the
    §8.2 recipe (0002, CIC) really runs concurrently and 0003 sees the migrator's 5s."""
    schemas = [("pe2e", "tenant_1")]

    def test_provision_then_reentry_is_noop(self) -> None:
        t = Tenant.objects.create(schema_name="pe2e", company_name="pe2e", shard=self.s1)
        code, doc = self.run_cmd("-s", "pe2e")
        self.assertEqual(code, 0, doc)
        (r,) = doc["results"]
        self.assertEqual((r["kind"], r["reason"], r["invalid_indexes"]), ("ok", "applied", []))
        t.refresh_from_db()
        self.assertEqual(t.status, "active")
        self.assertEqual(self.scalar("tenant_1", "SELECT value FROM pe2e.canary_probe"),
                         settings.TENANT_MIGRATE_DDL_LOCK_TIMEOUT)
        self.assertTrue(self.scalar(
            "tenant_1", "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                        " JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = 'pe2e' AND c.relname = 'canary_item_name_0002_idx'"))
        self.assertEqual(migrator.pending_plan(Tenant.objects.select_related("shard").get(pk=t.pk)), [])

        # ReentryE2E: a second run finds nothing to do and takes no lease.
        code, doc = self.run_cmd("-s", "pe2e")
        self.assertEqual((code, doc["results"][0]["kind"], doc["results"][0]["reason"]),
                         (0, "noop", "up_to_date"))


@_CANARY
class LockTimeoutE2E(_E2ECase):
    """A reader holds ACCESS SHARE on canary_item; 0004's ADD COLUMN must give up after
    lock_timeout instead of queueing for ever — and while it waits, a third session's
    SELECT is stuck behind it no longer than that timeout."""
    schemas = [("lte2e", "tenant_1")]

    def test_add_column_times_out_then_succeeds(self) -> None:
        t = Tenant.objects.create(schema_name="lte2e", company_name="lte2e", shard=self.s1)
        with override_settings(MIGRATION_MODULES={"canary": "tenants.tests.canary.migrations_upto3"}):
            res = migrator.migrate_tenant(t.pk, migrator.RunOptions(verbosity=0))
        self.assertEqual(res.kind, "ok", res)
        t = Tenant.objects.select_related("shard").get(pk=t.pk)
        self.assertEqual(migrator.pending_plan(t), [("canary", "0004_item_extra")])

        holder = _connect("tenant_1")                       # BEGIN implicitly
        holder.execute('SELECT * FROM "lte2e".canary_item')
        third: dict = {}

        def third_reader() -> None:
            with _connect("tenant_1", autocommit=True) as c:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:          # until ALTER is queued
                    if c.execute("SELECT 1 FROM pg_locks WHERE NOT granted"
                                 " AND mode = 'AccessExclusiveLock'").fetchone():
                        break
                    time.sleep(0.05)
                else:
                    return
                started = time.monotonic()
                c.execute('SELECT count(*) FROM "lte2e".canary_item')
                third["blocked"] = time.monotonic() - started
        reader = threading.Thread(target=third_reader)
        try:
            reader.start()
            started = time.monotonic()
            code, doc = self.run_cmd("-s", "lte2e")
            took = time.monotonic() - started
            reader.join(15)
        finally:
            holder.rollback()
            holder.close()

        (r,) = doc["results"]
        self.assertEqual((code, r["kind"], r["reason"]), (1, "failed", "lock_timeout"), r)
        timeout = float(settings.TENANT_MIGRATE_DDL_LOCK_TIMEOUT.rstrip("s"))
        self.assertLess(abs(r["secs"] - timeout), 1.5, r)
        self.assertLess(took, timeout + 5)
        self.assertEqual(Tenant.objects.get(pk=t.pk).status, "active")   # upgrade: status kept
        self.assertIn("blocked", third, "the ALTER never queued for its lock")
        self.assertGreater(third["blocked"], 0.5)                      # really queued behind it
        self.assertLess(third["blocked"], timeout + 1)                 # …but only until it gave up

        code, doc = self.run_cmd("-s", "lte2e")                        # the reader is gone
        self.assertEqual((code, doc["results"][0]["kind"]), (0, "ok"), doc)
        self.assertEqual(self.scalar("tenant_1", "SELECT count(*) FROM information_schema.columns"
                                                 " WHERE table_schema = 'lte2e'"
                                                 " AND table_name = 'canary_item' AND column_name = 'extra'"), 1)


class CeleryConnectionHygieneTests(_E2ECase):
    """§7, I8: a long-lived Celery worker runs provision_tenant, then other tasks on the
    SAME connections — none may keep the migrator's lock_timeout or its tm:<pk> label,
    after a success or after a failure. Same for `default` and for --shared."""
    schemas = [("hyg_ok", "tenant_1"), ("hyg_bad", "tenant_1")]

    def server_default(self, alias: str) -> str:
        with _connect(alias, autocommit=True) as c:
            return c.execute("SELECT reset_val FROM pg_settings WHERE name = 'lock_timeout'").fetchone()[0]

    def assert_clean(self, alias: str) -> None:
        if alias != "default":
            # The shard connection is CLOSED, not reset (§7). `default` may be open: _fresh()
            # closes it BEFORE each migrator access, and the last one (the lease release)
            # leaves a plain connection that never carried the migrator's settings.
            self.assertIsNone(connections[alias].connection, f"{alias} left open")
        with connections[alias].cursor() as cur:            # what the next task gets
            cur.execute("SHOW lock_timeout")
            lt = cur.fetchone()[0]
            cur.execute("SHOW application_name")
            app = cur.fetchone()[0]
        self.assertEqual(lt, self.server_default(alias), alias)
        self.assertFalse(app.startswith("tm:"), (alias, app))
        connections[alias].close()

    def test_provision_success_failure_and_shared(self) -> None:
        from tenants.tasks import provision_tenant
        ok = Tenant.objects.create(schema_name="hyg_ok", company_name="hyg_ok", shard=self.s1)
        out = provision_tenant.apply(args=[ok.pk]).get()
        self.assertEqual(out["kind"], "ok", out)
        self.assert_clean("tenant_1")
        self.assert_clean("default")

        bad = Tenant.objects.create(schema_name="hyg_bad", company_name="hyg_bad", shard=self.s1)
        with mock.patch.object(migrator, "run_migrations", side_effect=ValueError("boom")):
            out = provision_tenant.apply(args=[bad.pk]).get()
        self.assertEqual((out["kind"], out["reason"]), ("failed", "migration_error"), out)
        self.assertEqual(Tenant.objects.get(pk=bad.pk).status, "failed")
        self.assert_clean("tenant_1")
        self.assert_clean("default")

        res = migrator.migrate_shared(migrator.RunOptions(verbosity=0))
        self.assertEqual(res.kind, "ok", res)
        self.assert_clean("default")


class MaintenanceEntryRaceE2E(_E2ECase):
    """Review 2026-10-13: the admin writes MAINTENANCE in its own (uncommitted) transaction;
    a migrator reaching the same tenant at that moment must wait on the lease row and then
    skip it — never start upgrading a tenant that is being handed to an operator."""
    schemas = [("mrace", "tenant_1")]

    def test_migrator_waits_for_the_admin_and_skips_the_tenant(self) -> None:
        from django.db import transaction
        t = Tenant.objects.create(schema_name="mrace", company_name="mrace", shard=self.s1)
        Tenant.objects.filter(pk=t.pk).update(status="active", previous_status="active")
        with connections["tenant_1"].cursor() as cur:            # empty schema: a full plan is due
            cur.execute('CREATE SCHEMA "mrace"')
        locked, release = threading.Event(), threading.Event()

        def admin() -> None:                                    # what TenantAdmin.save_model does
            try:
                with transaction.atomic():
                    self.assertIsNone(migrator.lease_lock_for_transition(t.pk))
                    Tenant.objects.filter(pk=t.pk, status="active").update(
                        status="maintenance", previous_status="active")
                    locked.set()
                    release.wait(10)
            finally:
                connections.close_all()
        th = threading.Thread(target=admin)
        th.start()
        self.assertTrue(locked.wait(10))
        threading.Timer(1.0, release.set).start()               # the admin commits in 1 s
        started = time.monotonic()
        with mock.patch.object(migrator, "run_migrations") as run:
            res = migrator.migrate_tenant(t.pk, migrator.RunOptions(verbosity=0))
        waited = time.monotonic() - started
        th.join(10)
        run.assert_not_called()
        self.assertEqual((res.kind, res.reason), ("skipped", "status_maintenance"), res)
        self.assertGreater(waited, 0.7)                         # it really waited on the row lock
        self.assertIsNone(migrator.lease_holder(t.pk))          # and released what it took


class ParallelE2E(TransactionTestCase):
    """--jobs=2, 4 NEW tenants on 2 shards, from the CLI path: every tenant provisioned
    by a forked worker, exit 0, and the workers' lines on the shared stdout stay whole."""
    databases = ALL_DBS

    def setUp(self) -> None:
        s1, s2 = _make_shards()
        self.specs = [("pe_a", s1), ("pe_b", s2), ("pe_c", s1), ("pe_d", s2)]
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = override_settings(TENANT_MIGRATE_LOG_DIR=self.tmp / "logs")
        patcher.enable()
        self.addCleanup(patcher.disable)
        forget = mock.patch.object(migrator.resolve_cache, "forget_tenant")
        forget.start()
        self.addCleanup(forget.stop)
        self.addCleanup(self._drop_schemas)

    def _drop_schemas(self) -> None:
        for schema, shard in self.specs:
            with connections[shard.alias].cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {quote_schema(schema)} CASCADE")

    def _run_capturing_fd1(self, *args) -> tuple[int, str, str]:
        """Workers inherit fd 1, not this process's sys.stdout object: capture the fd."""
        cmd = load_command_class("tenants", "migrate_schemas")
        cmd._cli = True
        out = io.StringIO()
        screen = self.tmp / "fd1.txt"
        sys.stdout.flush()
        saved = os.dup(1)
        with open(screen, "wb") as f:
            os.dup2(f.fileno(), 1)
            try:
                call_command(cmd, *args, stdout=out, stderr=io.StringIO())
                code = 0
            except CommandError as e:
                code = e.returncode
            finally:
                sys.stdout.flush()
                os.dup2(saved, 1)
                os.close(saved)
        return code, out.getvalue(), screen.read_text()

    def test_two_jobs_four_tenants_two_shards(self) -> None:
        for schema, shard in self.specs:
            Tenant.objects.create(schema_name=schema, company_name=schema, shard=shard)
        summary = self.tmp / "s.json"
        code, out, screen = self._run_capturing_fd1("--tenant", "--jobs=2", f"--summary-json={summary}")
        self.assertEqual(code, 0, out)

        doc = json.loads(summary.read_text())
        self.assertEqual(doc["options"]["jobs"], 2)
        self.assertEqual(sorted((r["schema"], r["shard"], r["kind"], r["reason"]) for r in doc["results"]),
                         [("pe_a", "tenant_1", "ok", "applied"), ("pe_b", "tenant_2", "ok", "applied"),
                          ("pe_c", "tenant_1", "ok", "applied"), ("pe_d", "tenant_2", "ok", "applied")])
        for schema, shard in self.specs:
            t = Tenant.objects.select_related("shard").get(schema_name=schema)
            self.assertEqual(t.status, "active")
            self.assertEqual(migrator.pending_plan(t), [])
        self.assertFalse(MigrationLease.objects.exclude(owner=None).exists())

        run = Path(doc["log_dir"])
        logs = {p.stem: p.read_text() for p in run.glob("pe_*.log")}
        self.assertEqual(sorted(logs), ["pe_a", "pe_b", "pe_c", "pe_d"])
        owners = {next(l for l in text.splitlines() if l.startswith("lease owner: "))
                  for text in logs.values()}
        self.assertNotIn(f"lease owner: {migrator.owner()}", owners)    # forked workers did it
        self.assertLessEqual(len(owners), 2)

        # Line buffering: every line the workers printed is a whole line of some tenant's
        # file (the files hold exactly what each worker printed, line by line).
        file_lines = {l for text in logs.values() for l in text.splitlines()}
        printed = [l for l in screen.splitlines() if l.strip()]
        self.assertTrue(any("Applying users.0001_initial..." in l for l in printed), screen[:500])
        torn = [l for l in printed if migrator._clean(l) not in file_lines]
        self.assertEqual(torn, [])
