"""TasksTests (deploy/migrations_plan_rev7.md §15.1, §20.3): provision_tenant calls the
migrator core directly, retries `locked` / `code_older_than_db`, never retries a failure.
DB-free: the Tenant lookup and migrate_tenant are mocked."""
from types import SimpleNamespace
from unittest import mock

from celery.exceptions import Retry
from django.test import SimpleTestCase

from tenants import migrator, tasks
from tenants.migrator import Reason, Result
from tenants.models import Tenant


class TasksTests(SimpleTestCase):
    def run_task(self, result: Result | None, status: str = "new"):
        tenant = None if status is None else SimpleNamespace(status=status)
        lookup = mock.Mock(**{"first.return_value": tenant})
        with mock.patch.object(Tenant.objects, "filter", return_value=lookup), \
             mock.patch.object(migrator, "migrate_tenant", return_value=result) as core, \
             mock.patch.object(tasks, "call_command") as cc, \
             mock.patch.object(tasks.provision_tenant, "retry", side_effect=Retry("again")) as retry:
            try:
                out = tasks.provision_tenant.run(7)
            except Retry:
                out = "RETRY"
        cc.assert_not_called()                          # no call_command("migrate_schemas")
        return out, core, retry

    def test_direct_core_call(self) -> None:
        out, core, retry = self.run_task(Result("ok", "acme", "tenant_1", Reason.APPLIED, "provisioned"))
        core.assert_called_once_with(7, migrator.RunOptions())
        self.assertIsNone(core.call_args.args[1].log_dir)            # no log files from Celery
        retry.assert_not_called()
        self.assertEqual((out["kind"], out["reason"], out["schema"]), ("ok", "applied", "acme"))

    def test_pending_is_taken_too(self) -> None:
        out, core, _ = self.run_task(Result("ok", "acme", "tenant_1", Reason.RESUMED), status="pending")
        core.assert_called_once()
        self.assertEqual(out["reason"], "resumed")

    def test_locked_and_code_older_retry(self) -> None:
        for reason in (Reason.LOCKED, Reason.CODE_OLDER_THAN_DB):
            out, _, retry = self.run_task(Result("skipped", "acme", "tenant_1", reason, "x"))
            self.assertEqual(out, "RETRY", reason)
            retry.assert_called_once_with(countdown=300, max_retries=15)

    def test_failed_and_other_skips_do_not_retry(self) -> None:
        for res in (Result("failed", "acme", "tenant_1", Reason.MIGRATION_ERROR, "boom"),
                    Result("failed", "acme", "tenant_1", Reason.CONN_ERROR, "down", conn_error="shard"),
                    Result("skipped", "acme", "tenant_1", Reason.STATUS_FAILED, "x")):
            with self.assertLogs("tenants.tasks", "INFO") if res.kind == "failed" else self.assertNoLogs("tenants.tasks", "ERROR"):
                out, _, retry = self.run_task(res)
            retry.assert_not_called()
            self.assertEqual(out["kind"], res.kind)

    def test_not_provisionable_is_skipped_without_the_core(self) -> None:
        for status in ("active", "deactivated", "failed", "maintenance", None):
            out, core, _ = self.run_task(None, status=status)
            core.assert_not_called()
            self.assertEqual((out["kind"], out["status"]), ("skipped", status or "deleted"))

    def test_retry_budget_is_declared_on_the_task(self) -> None:
        self.assertEqual(tasks.provision_tenant.max_retries, 15)
        self.assertTrue(tasks.provision_tenant.acks_late)
