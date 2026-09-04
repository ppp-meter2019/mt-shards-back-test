"""Tenant/Shard model invariants. DB-free: the read-only guard raises before
super().save()/delete() touch the DB, and the status_changed_at tests stub out
Model.save so only the stamping DECISION is exercised."""
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.db import models
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


class StatusChangedAtTests(SimpleTestCase):
    """status_changed_at must track the STATUS, not the row.

    The old auto_now=True did the opposite: it fired on every .save() (so an edit of
    `description` shifted it) and never on QuerySet.update() (where every status writer
    lives). Tenant.save() now stamps it iff the status actually moved.
    """

    OLD = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)

    def _loaded(self, status=Tenant.Status.ACTIVE):
        """A Tenant as it comes back from the DB: not adding, with _loaded_status set."""
        t = Tenant(id=5, schema_name="alpha", company_name="Alpha", status=status,
                   status_changed_at=self.OLD)
        t._state.adding = False
        t._loaded_status = status
        return t

    @staticmethod
    def _save(tenant, **kwargs):
        """Run Tenant.save() with the real DB write stubbed out; return the kwargs that
        reached Model.save (so update_fields extension is observable)."""
        with mock.patch.object(models.Model, "save", autospec=True) as inner:
            tenant.save(**kwargs)
        return inner.call_args.kwargs

    def test_unrelated_edit_does_not_move_it(self):
        t = self._loaded()
        t.description = "just a note"          # the auto_now regression this replaces
        self._save(t)
        self.assertEqual(t.status_changed_at, self.OLD)

    def test_status_change_stamps_it(self):
        t = self._loaded(Tenant.Status.ACTIVE)
        t.status = Tenant.Status.DEACTIVATED
        self._save(t)
        self.assertGreater(t.status_changed_at, self.OLD)

    def test_insert_stamps_it(self):
        t = Tenant(schema_name="beta", company_name="Beta", status=Tenant.Status.NEW,
                   status_changed_at=self.OLD)
        self.assertTrue(t._state.adding)
        self._save(t)
        self.assertGreater(t.status_changed_at, self.OLD)

    def test_update_fields_is_extended(self):
        """Otherwise the new value is computed and then silently not written — e.g.
        scripts/resolve_cache_bench.py does save(update_fields=["status"])."""
        t = self._loaded(Tenant.Status.ACTIVE)
        t.status = Tenant.Status.DEACTIVATED
        passed = self._save(t, update_fields=["status"])
        self.assertEqual(set(passed["update_fields"]), {"status", "status_changed_at"})

    def test_update_fields_untouched_when_status_unchanged(self):
        t = self._loaded()
        t.description = "note"
        passed = self._save(t, update_fields=["description"])
        self.assertEqual(list(passed["update_fields"]), ["description"])

    def test_from_db_snapshots_status(self):
        names = ["id", "schema_name", "company_name", "status"]
        t = Tenant.from_db(None, names, [5, "alpha", "Alpha", Tenant.Status.ACTIVE])
        self.assertEqual(t._loaded_status, Tenant.Status.ACTIVE)

    def test_from_db_tolerates_deferred_status(self):
        """A .only()/.defer() load must not make from_db refetch the column."""
        t = Tenant.from_db(None, ["id", "schema_name"], [5, "alpha"])
        self.assertFalse(hasattr(t, "_loaded_status"))

    def test_field_is_not_auto_now(self):
        f = Tenant._meta.get_field("status_changed_at")
        self.assertFalse(f.auto_now)           # auto_now would silently restore the old bug
        self.assertFalse(f.auto_now_add)
