"""TenantViewSet queues the right Celery task. DB-free (mocks, no ORM).

These exist because both call sites shipped broken: they said `from .tasks import ...`, which
resolves to tenants.console.tasks — a module that has never existed. The tasks live in
tenants/tasks.py. Nothing caught it, because nothing called either path: `provision` 500'd on
every request, and `perform_destroy` only reached the import under ?drop_schema=true, the one
branch an operator rarely takes.

What is asserted is the TASK, not the import: a test that merely imported the module would
have passed just as happily with the view still broken. Each case drives the real viewset
method and checks that .delay() was called with the right arguments — so the import has to
resolve for the assertion to be reachable at all.
"""
from unittest import mock

from django.test import SimpleTestCase

from tenants.console.views import TenantViewSet
from tenants.models import Tenant


def _tenant(schema="acme", alias="tenant_1", status=Tenant.Status.NEW, pk=7):
    """A Tenant stand-in: the viewset only reads these four attributes and calls delete()."""
    t = mock.Mock(spec_set=["schema_name", "shard", "status", "id", "delete"])
    t.schema_name, t.status, t.id = schema, status, pk
    t.shard = mock.Mock(alias=alias)
    return t


class DropSchemaTaskTests(SimpleTestCase):
    def _run(self, drop_param):
        view = TenantViewSet()
        view.request = mock.Mock(query_params={"drop_schema": drop_param} if drop_param else {})
        instance = _tenant()
        with mock.patch("tenants.tasks.drop_tenant_schema_task.delay") as delay:
            view.perform_destroy(instance)
        return instance, delay

    def test_drop_schema_true_queues_the_task_with_shard_and_schema(self):
        """The task takes the shard alias and the schema name, both captured BEFORE delete()
        (the instance is gone afterwards)."""
        instance, delay = self._run("true")
        instance.delete.assert_called_once_with()
        delay.assert_called_once_with("tenant_1", "acme")

    def test_without_the_flag_the_row_goes_but_no_task_is_queued(self):
        """Dropping the schema is opt-in: auto_drop_schema is off, so the default DELETE
        leaves the schema in place on purpose."""
        instance, delay = self._run(None)
        instance.delete.assert_called_once_with()
        delay.assert_not_called()

    def test_the_flag_accepts_the_documented_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                _, delay = self._run(value)
                delay.assert_called_once_with("tenant_1", "acme")

    def test_public_tenant_is_refused_before_anything_is_deleted(self):
        from rest_framework.exceptions import PermissionDenied
        view = TenantViewSet()
        view.request = mock.Mock(query_params={"drop_schema": "true"})
        instance = _tenant(schema="public")
        with mock.patch("tenants.tasks.drop_tenant_schema_task.delay") as delay:
            with self.assertRaises(PermissionDenied):
                view.perform_destroy(instance)
        instance.delete.assert_not_called()
        delay.assert_not_called()


class ProvisionTaskTests(SimpleTestCase):
    def _run(self, status):
        view = TenantViewSet()
        tenant = _tenant(status=status)
        with mock.patch.object(TenantViewSet, "get_object", return_value=tenant), \
             mock.patch("tenants.tasks.provision_tenant.delay") as delay:
            response = view.provision(mock.Mock(), pk="7")
        return tenant, response, delay

    def test_new_tenant_queues_provisioning(self):
        tenant, response, delay = self._run(Tenant.Status.NEW)
        delay.assert_called_once_with(tenant.id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["schema"], "acme")

    def test_already_provisioned_is_refused_without_queueing(self):
        """Only a NEW tenant is provisionable — re-provisioning an ACTIVE one would re-run
        CREATE SCHEMA and migrations against live data."""
        _, response, delay = self._run(Tenant.Status.ACTIVE)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "not_provisionable")
        delay.assert_not_called()
