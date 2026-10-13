"""DB-backed tests for step 7 of deploy/migrations_plan_rev7.md §21: the reactivation
stopper (§5.5), the admin (§14.2, AdminTests), the console (§14.3, ConsoleStopperTests)
and the MAINTENANCE rules of both (rest of MaintenanceTests).

No ``test_`` prefix on purpose (see db_migrator_core.py). Run:

    DJANGO_SETTINGS_MODULE=tenants.tests.settings_db \\
        ../p_env/bin/python manage.py test tenants.tests.db_admin_console
"""
from types import SimpleNamespace
from typing import Any
from unittest import mock

from django.db import OperationalError, transaction
from django.test import RequestFactory, override_settings
from rest_framework.test import APIRequestFactory

from tenants import migrator
from tenants.console.admin import (STATUS_TRANSITIONS, StatusConflict, TenantAdmin,
                                   public_admin_site)
from tenants.console.views import TenantViewSet
from tenants.models import Tenant
from tenants.tests.db_migrator_core import OTHER, _MigratorCase, _create_schema, _put_lease

S = Tenant.Status


class _Case(_MigratorCase):
    def migrated(self, schema: str, status: str = S.ACTIVE) -> Tenant:
        """A really provisioned tenant (schema up to date), then moved to `status`."""
        t = self.tenant(schema)
        self.assertEqual(migrator.migrate_tenant(t.pk, migrator.RunOptions(verbosity=0)).kind, "ok")
        Tenant.objects.filter(pk=t.pk).update(status=status, previous_status=S.ACTIVE)
        self.forget.reset_mock()
        return Tenant.objects.select_related("shard").get(pk=t.pk)


class CheckReactivationTests(_Case):
    def test_outcomes(self) -> None:
        ok = self.migrated("cr_ok", S.DEACTIVATED)
        migrator.check_reactivation(ok)                                 # no exception

        missing = self.tenant("cr_missing", status=S.DEACTIVATED)
        with self.assertRaises(migrator.SchemaMissing) as ctx:
            migrator.check_reactivation(missing)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("schema_missing", 409))

        behind = self.tenant("cr_behind", status=S.DEACTIVATED)
        _create_schema("tenant_1", "cr_behind")
        with self.assertRaises(migrator.SchemaBehind) as ctx:
            migrator.check_reactivation(behind)
        self.assertIn("users.0001_initial", ctx.exception.pending)

        with mock.patch.object(migrator, "code_older_than_db", return_value=True), \
             self.assertRaises(migrator.DeployInProgress) as ctx:
            migrator.check_reactivation(ok)
        self.assertEqual(ctx.exception.status, 503)

        with mock.patch.object(migrator, "pending_plan", side_effect=OperationalError("down")), \
             self.assertRaises(migrator.ReactivationUnavailable) as ctx:
            migrator.check_reactivation(ok)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("shard_unreachable", 503))


# ---------------------------------------------------------------------------
class AdminTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.admin = TenantAdmin(Tenant, public_admin_site)
        self.request = RequestFactory().post("/")
        self.request.user = SimpleNamespace(is_active=True, is_staff=True, is_superuser=True,
                                            has_perm=lambda *a, **k: True)

    def form(self, t: Tenant, **changes: Any):
        """The admin's own form class for `t`, bound to its current values + `changes`."""
        Form = self.admin.get_form(self.request, obj=t, change=True)
        unbound = Form(instance=t)
        data = {n: unbound[n].value() for n in unbound.fields}
        data = {k: v for k, v in data.items() if v is not None and v is not False}
        data.update({k: v for k, v in changes.items() if v is not False})
        for k, v in changes.items():
            if v is False:
                data.pop(k, None)
        return Form(data=data, instance=t)

    def save(self, form) -> tuple[Tenant, list[str]]:
        self.assertTrue(form.is_valid(), form.errors)
        obj = self.admin.save_form(self.request, form, change=True)
        msgs: list[str] = []
        with mock.patch.object(self.admin, "message_user",
                               side_effect=lambda req, msg, **kw: msgs.append(msg)), \
             transaction.atomic():
            self.admin.save_model(self.request, obj, form, change=True)
        return Tenant.objects.get(pk=obj.pk), msgs

    def test_matrix(self) -> None:
        self.assertEqual(STATUS_TRANSITIONS[S.MAINTENANCE], [S.MAINTENANCE, S.ACTIVE, S.DEACTIVATED, S.FAILED])
        self.assertEqual(set(STATUS_TRANSITIONS), set(S.values))
        for status in (S.ACTIVE, S.DEACTIVATED, S.FAILED):
            self.assertIn(S.MAINTENANCE, STATUS_TRANSITIONS[status])
        t = self.tenant("ad_m", status=S.MAINTENANCE)
        choices = [c for c, _ in self.form(t).fields["status"].choices]
        self.assertEqual(choices, [S.MAINTENANCE, S.ACTIVE, S.DEACTIVATED, S.FAILED])
        new = self.tenant("ad_new")
        self.assertTrue(self.form(new).fields["status"].disabled)

    def test_to_active_goes_through_the_stopper(self) -> None:
        behind = self.tenant("ad_behind", status=S.DEACTIVATED)
        _create_schema("tenant_1", "ad_behind")
        f = self.form(behind, status=S.ACTIVE)
        self.assertFalse(f.is_valid())
        self.assertIn("migrate_schemas -s ad_behind", f.errors["status"][0])
        maint = self.tenant("ad_maint", status=S.MAINTENANCE)
        f = self.form(maint, status=S.ACTIVE)
        self.assertFalse(f.is_valid())
        self.assertIn("migrate_schemas -s ad_maint --maintenance", f.errors["status"][0])

        ok = self.migrated("ad_ok", S.MAINTENANCE)
        t, msgs = self.save(self.form(ok, status=S.ACTIVE))
        self.assertEqual((t.status, t.previous_status, msgs), (S.ACTIVE, S.MAINTENANCE, []))

    def test_failed_to_new_needs_the_confirmation(self) -> None:
        t = self.tenant("ad_failed", status=S.FAILED)
        Tenant.objects.filter(pk=t.pk).update(last_error="boom")
        t = Tenant.objects.get(pk=t.pk)
        _create_schema("tenant_1", "ad_failed")
        f = self.form(t, status=S.NEW)
        self.assertIn("Schema now: empty, on tenant_1", f.fields["confirm_schema"].help_text)
        self.assertFalse(f.is_valid())
        self.assertIn("confirm_schema", f.errors)
        t = Tenant.objects.get(pk=t.pk)         # the admin builds each request's form on a fresh row
        t, _ = self.save(self.form(t, status=S.NEW, confirm_schema="on"))
        self.assertEqual((t.status, t.previous_status, t.last_error), (S.NEW, S.FAILED, ""))
        self.assertNotIn("confirm_schema", self.admin.get_fields(self.request, t))

    def test_maintenance_refused_while_a_migrator_holds_the_lease(self) -> None:
        t = self.migrated("ad_lease")
        _put_lease(t.pk, OTHER, "30 minutes")
        f = self.form(t, status=S.MAINTENANCE)
        self.assertFalse(f.is_valid())
        self.assertIn(f"Being migrated right now by {OTHER}", f.errors["status"][0])
        _put_lease(t.pk, OTHER, "-1 minute")                # expired, no backend → allowed
        t = Tenant.objects.get(pk=t.pk)
        t2, _ = self.save(self.form(t, status=S.MAINTENANCE))
        self.assertEqual((t2.status, t2.previous_status), (S.MAINTENANCE, S.ACTIVE))

    def test_maintenance_refused_when_a_migrator_got_the_lease_after_clean(self) -> None:
        """The form saw no lease; a migrator takes it before save_model's row lock."""
        t = self.migrated("ad_race_m")
        f = self.form(t, status=S.MAINTENANCE)
        self.assertTrue(f.is_valid(), f.errors)
        _put_lease(t.pk, OTHER, "30 minutes")
        with self.assertRaisesMessage(StatusConflict, f"started migrating meanwhile ({OTHER}"):
            self.save(f)
        self.assertEqual(Tenant.objects.get(pk=t.pk).status, S.ACTIVE)

    def test_lease_lock_needs_a_transaction(self) -> None:
        from django.db import connections
        with mock.patch.object(connections["default"], "in_atomic_block", False), \
             self.assertRaisesMessage(RuntimeError, "inside transaction.atomic()"):
            migrator.lease_lock_for_transition(1)

    def test_cas_lost_raises_status_conflict(self) -> None:
        t = self.migrated("ad_cas", S.DEACTIVATED)
        f = self.form(t, status=S.MAINTENANCE, company_name="Renamed")
        self.assertTrue(f.is_valid(), f.errors)
        Tenant.objects.filter(pk=t.pk).update(status=S.ACTIVE)    # moved after clean()
        with self.assertRaisesMessage(StatusConflict, "changed concurrently"):
            self.save(f)
        self.assertEqual(Tenant.objects.get(pk=t.pk).status, S.ACTIVE)

    def test_status_change_forgets_on_commit_and_other_fields_never_write_status(self) -> None:
        t = self.migrated("ad_fields")
        f = self.form(t, company_name="Renamed")
        self.assertTrue(f.is_valid(), f.errors)
        Tenant.objects.filter(pk=t.pk).update(status=S.DEACTIVATED)   # concurrent writer
        t2, _ = self.save(f)
        self.assertEqual((t2.company_name, t2.status), ("Renamed", S.DEACTIVATED))   # not resurrected

        t3 = self.migrated("ad_forget")
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.save(self.form(t3, status=S.DEACTIVATED))
        self.assertTrue(callbacks)
        self.assertIn(t3.pk, [c.args[0].pk for c in self.forget.call_args_list])


# ---------------------------------------------------------------------------
class ConsoleStopperTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        p = mock.patch.object(TenantViewSet, "permission_classes", [])
        p.start()
        self.addCleanup(p.stop)

    def call(self, action: str, t: Tenant, method: str = "post"):
        view = TenantViewSet.as_view({method: action})
        req = getattr(APIRequestFactory(), method)(f"/api/v1/tenants/{t.pk}/")
        return view(req, pk=str(t.pk))

    def test_activate_codes(self) -> None:
        missing = self.tenant("cs_missing", status=S.DEACTIVATED)
        r = self.call("activate", missing)
        self.assertEqual((r.status_code, r.data["code"]), (409, "schema_missing"))

        behind = self.tenant("cs_behind", status=S.DEACTIVATED)
        _create_schema("tenant_1", "cs_behind")
        r = self.call("activate", behind)
        self.assertEqual((r.status_code, r.data["code"]), (409, "schema_behind"))
        self.assertIn("users.0001_initial", r.data["pending"])
        self.assertIn("migrate_schemas -s cs_behind", r.data["detail"])

        ok = self.migrated("cs_ok", S.DEACTIVATED)
        with mock.patch.object(migrator, "code_older_than_db", return_value=True):
            r = self.call("activate", ok)
        self.assertEqual((r.status_code, r.data["code"]), (503, "deploy_in_progress"))
        with mock.patch.object(migrator, "pending_plan", side_effect=OperationalError("down")):
            r = self.call("activate", ok)
        self.assertEqual((r.status_code, r.data["code"]), (503, "shard_unreachable"))
        self.assertEqual(Tenant.objects.get(pk=ok.pk).status, S.DEACTIVATED)

        r = self.call("activate", ok)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Tenant.objects.get(pk=ok.pk).status, S.ACTIVE)

    def test_maintenance_is_409_for_every_console_write(self) -> None:
        t = self.tenant("cs_maint", status=S.MAINTENANCE)
        for action in ("activate", "deactivate"):
            r = self.call(action, t)
            self.assertEqual((r.status_code, r.data["code"]), (409, "tenant_maintenance"), action)
        r = self.call("destroy", t, method="delete")
        self.assertEqual((r.status_code, r.data["code"]), (409, "tenant_maintenance"))
        self.assertIn("finish maintenance first", r.data["detail"])
        self.assertTrue(Tenant.objects.filter(pk=t.pk, status=S.MAINTENANCE).exists())

    def test_deactivate_needs_no_stopper(self) -> None:
        t = self.tenant("cs_deact", status=S.ACTIVE)            # no schema at all
        r = self.call("deactivate", t)
        self.assertEqual(r.status_code, 200, r.data)


# ---------------------------------------------------------------------------
@override_settings(ROOT_URLCONF="tenants_back.urls_public")   # the platform admin lives on the public host
class AdminChangeViewTests(_Case):
    """The WHOLE admin change view (POST → changeform_view → save_model → save_related →
    log_change), not save_model alone: a conflict must end in a message, never a 500."""

    def setUp(self) -> None:
        super().setUp()
        from django.contrib.auth import get_user_model
        from tenants.context import use_alias
        self.admin = TenantAdmin(Tenant, public_admin_site)
        self.public = lambda: use_alias("default")   # what TenantShardRoutingMiddleware does on the public host
        with self.public():
            self.user = get_user_model().objects.create_superuser("root_t", "r@x.io", "pw")

    def get_data(self, t: Tenant) -> dict[str, Any]:
        """What the change page rendered on GET (incl. the hidden expected_status)."""
        req = RequestFactory().get("/")
        req.user = self.user
        Form = self.admin.get_form(req, obj=t, change=True)
        unbound = Form(instance=t)
        data = {n: unbound[n].value() for n in unbound.fields}
        return {k: v for k, v in data.items() if v is not None and v is not False}

    def post(self, t: Tenant, data: dict[str, Any]):
        from django.contrib.messages.storage.cookie import CookieStorage
        req = RequestFactory().post(f"/admin/tenants/tenant/{t.pk}/change/", {**data, "_save": "1"})
        req.user = self.user
        req._dont_enforce_csrf_checks = True
        req._messages = CookieStorage(req)
        with self.public():
            resp = self.admin.changeform_view(req, str(t.pk))
        return resp, [str(m) for m in req._messages]

    def log_count(self) -> int:
        from django.contrib.admin.models import LogEntry
        with self.public():
            return LogEntry.objects.count()

    def test_status_moved_before_post_is_a_form_error(self) -> None:
        t = self.migrated("cv_stale", S.DEACTIVATED)
        data = {**self.get_data(t), "status": S.MAINTENANCE, "company_name": "Renamed"}
        Tenant.objects.filter(pk=t.pk).update(status=S.ACTIVE)    # console activated it
        resp, _ = self.post(t, data)
        self.assertEqual(resp.status_code, 200)                    # page re-rendered
        errors = resp.context_data["adminform"].form.non_field_errors()
        self.assertIn("changed while this page was open", errors[0])
        row = Tenant.objects.get(pk=t.pk)
        self.assertEqual((row.status, row.company_name), (S.ACTIVE, "Cv_Stale"))

    def test_stale_page_cannot_silently_revert_a_concurrent_change(self) -> None:
        """Opened as ACTIVE, deactivated in the console, then saved with status ACTIVE on
        the page: no status change was REQUESTED, so the status stays DEACTIVATED."""
        t = self.migrated("cv_revert", S.ACTIVE)
        data = {**self.get_data(t), "company_name": "Renamed"}
        Tenant.objects.filter(pk=t.pk).update(status=S.DEACTIVATED)
        resp, _ = self.post(t, data)
        self.assertEqual(resp.status_code, 302)
        row = Tenant.objects.get(pk=t.pk)
        self.assertEqual((row.status, row.company_name), (S.DEACTIVATED, "Renamed"))

    def test_editing_other_fields_keeps_a_concurrent_status_change_timestamp(self) -> None:
        """Opened as ACTIVE; the migrator writes FAILED with its own status_changed_at; the
        operator renames. Status AND its timestamp must stay the migrator's."""
        from datetime import datetime, timezone as tz
        t = self.migrated("cv_stamp", S.ACTIVE)
        data = {**self.get_data(t), "company_name": "Renamed"}
        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz.utc)
        Tenant.objects.filter(pk=t.pk).update(status=S.FAILED, status_changed_at=stamp)
        resp, _ = self.post(t, data)
        self.assertEqual(resp.status_code, 302)
        row = Tenant.objects.get(pk=t.pk)
        self.assertEqual((row.company_name, row.status, row.status_changed_at),
                         ("Renamed", S.FAILED, stamp))

    def test_cas_lost_after_clean_is_a_message_not_a_500(self) -> None:
        t = self.migrated("cv_race", S.DEACTIVATED)
        data = {**self.get_data(t), "status": S.MAINTENANCE, "company_name": "Renamed"}
        real = TenantAdmin.save_model

        def racing(admin_self, request, obj, form, change):
            # A concurrent writer between clean() and the CAS. It runs inside the admin's
            # own transaction here (one connection in a test), so it is rolled back with it;
            # a real one would commit on its own connection — the CAS loses either way.
            Tenant.objects.filter(pk=obj.pk).update(status=S.ACTIVE)
            return real(admin_self, request, obj, form, change)
        before = self.log_count()
        with mock.patch.object(TenantAdmin, "save_model", racing):
            resp, msgs = self.post(t, data)
        self.assertEqual((resp.status_code, resp["Location"]), (302, f"/admin/tenants/tenant/{t.pk}/change/"))
        self.assertTrue(any("changed concurrently" in m for m in msgs), msgs)
        self.assertEqual(self.log_count(), before)                 # log_change never ran
        row = Tenant.objects.get(pk=t.pk)
        self.assertEqual((row.status, row.company_name), (S.DEACTIVATED, "Cv_Race"))   # all rolled back

    def test_normal_status_change(self) -> None:
        t = self.migrated("cv_ok", S.ACTIVE)
        before = self.log_count()
        with self.captureOnCommitCallbacks(execute=True):
            resp, _ = self.post(t, {**self.get_data(t), "status": S.DEACTIVATED})
        self.assertEqual(resp.status_code, 302)
        row = Tenant.objects.get(pk=t.pk)
        self.assertEqual((row.status, row.previous_status), (S.DEACTIVATED, S.ACTIVE))
        self.assertEqual(self.log_count(), before + 1)
        self.assertIn(t.pk, [c.args[0].pk for c in self.forget.call_args_list])


class ConsolePatchTests(_Case):
    """Review 2026-10-13: a console PATCH reads the row at the start of the request; it must
    write only the fields it changes, never its stale status over a concurrent change."""

    def test_patch_does_not_write_back_a_stale_status(self) -> None:
        from tenants.console.serializers import TenantSerializer
        t = self.migrated("cp_stale", S.ACTIVE)
        stale = Tenant.objects.select_related("shard").get(pk=t.pk)     # what get_object() read
        Tenant.objects.filter(pk=t.pk).update(status=S.MAINTENANCE, previous_status=S.ACTIVE,
                                              last_error="restoring")    # admin, meanwhile
        ser = TenantSerializer(stale, data={"company_name": "Renamed"}, partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)
        ser.save()
        row = Tenant.objects.get(pk=t.pk)
        self.assertEqual((row.company_name, row.status, row.previous_status, row.last_error),
                         ("Renamed", S.MAINTENANCE, S.ACTIVE, "restoring"))
