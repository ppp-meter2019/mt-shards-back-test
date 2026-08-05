"""Read-only request-snapshot guard on Tenant/Shard. DB-free: the guard raises
before super().save()/delete() touch the DB."""
from django.test import SimpleTestCase

from tenants.models import ReadOnlyInstanceError, Shard, Tenant


class ReadOnlyGuardTests(SimpleTestCase):
    def _snapshot(self):
        t = Tenant(id=5, schema_name="alpha", status=Tenant.Status.ACTIVE)
        t.shard = Shard(id=2, alias="shard_a")
        t.read_only = True
        t.shard.read_only = True
        return t

    def test_tenant_save_and_delete_blocked(self):
        t = self._snapshot()
        with self.assertRaises(ReadOnlyInstanceError):
            t.save()
        with self.assertRaises(ReadOnlyInstanceError):
            t.delete()

    def test_shard_save_and_delete_blocked(self):
        s = self._snapshot().shard
        with self.assertRaises(ReadOnlyInstanceError):
            s.save()
        with self.assertRaises(ReadOnlyInstanceError):
            s.delete()

    def test_default_flag_is_false(self):
        self.assertFalse(Tenant.read_only)
        self.assertFalse(Shard.read_only)

    def test_error_subclasses_runtimeerror(self):
        self.assertTrue(issubclass(ReadOnlyInstanceError, RuntimeError))
