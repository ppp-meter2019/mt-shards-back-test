"""Tenant/Shard model invariants. DB-free: the read-only guard raises before
super().save()/delete() touch the DB, and the status_changed_at tests stub out
Model.save so only the stamping DECISION is exercised."""
from typing import Any
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.db import models
from django.test import SimpleTestCase

from tenants.models import Shard, Tenant


class SnapshotIsNotAModelTests(SimpleTestCase):
    """What replaced the `read_only` flag and its four save()/delete() overrides: the thing
    a request or a task holds is not a model at all, so there is nothing to guard."""

    def test_models_carry_no_read_only_flag(self) -> None:
        """Pinned so the flag is not reintroduced alongside the snapshot type — two
        mechanisms for one invariant is how they drift."""
        self.assertFalse(hasattr(Tenant, "read_only"))
        self.assertFalse(hasattr(Shard, "read_only"))

    def test_snapshot_cannot_be_saved_because_it_has_no_save(self) -> None:
        from tenants.resolver import TenantSnapshot
        snap = TenantSnapshot.capture(self._tenant())
        self.assertFalse(hasattr(snap, "save"))
        self.assertFalse(hasattr(snap, "delete"))

    def test_snapshot_exposes_only_routing_fields(self) -> None:
        """The uncarried fields must be ABSENT, not defaulted — a defaulted company_name is
        a plausible wrong value, an AttributeError is not."""
        from tenants.resolver import TenantSnapshot
        snap = TenantSnapshot.capture(self._tenant())
        self.assertEqual(snap.schema_name, "alpha")
        self.assertEqual(snap.shard.alias, "shard_a")
        for absent in ("company_name", "description", "last_error", "created_on"):
            with self.subTest(field=absent):
                self.assertFalse(hasattr(snap, absent))

    @staticmethod
    def _tenant() -> Tenant:
        t = Tenant(id=5, schema_name="alpha", company_name="Acme",
                   status=Tenant.Status.ACTIVE)
        t.shard = Shard(id=2, alias="shard_a", name="A")
        return t


class StatusChangedAtTests(SimpleTestCase):
    """status_changed_at must track the STATUS, not the row.

    auto_now=True would do the opposite: it fires on every .save() (so an edit of
    `description` would shift it) and never on QuerySet.update() — where every status writer
    lives. Hence Tenant.save() stamps it iff the status actually moved, and the .update()
    callers set it explicitly.
    """

    OLD = datetime(2020, 1, 1, tzinfo=dt_timezone.utc)

    def _loaded(self, status: str = Tenant.Status.ACTIVE) -> Tenant:
        """A Tenant as it comes back from the DB: not adding, with _loaded_status set."""
        t = Tenant(id=5, schema_name="alpha", company_name="Alpha", status=status,
                   status_changed_at=self.OLD)
        t._state.adding = False
        t._loaded_status = status
        return t

    @staticmethod
    def _save(tenant: Tenant, **kwargs: Any) -> dict[str, Any]:
        """Run Tenant.save() with the real DB write stubbed out; return the kwargs that
        reached Model.save (so update_fields extension is observable)."""
        with mock.patch.object(models.Model, "save", autospec=True) as inner:
            tenant.save(**kwargs)
        return inner.call_args.kwargs

    def test_unrelated_edit_does_not_move_it(self) -> None:
        t = self._loaded()
        t.description = "just a note"          # the auto_now regression this replaces
        self._save(t)
        self.assertEqual(t.status_changed_at, self.OLD)

    def test_status_change_stamps_it(self) -> None:
        t = self._loaded(Tenant.Status.ACTIVE)
        t.status = Tenant.Status.DEACTIVATED
        self._save(t)
        self.assertGreater(t.status_changed_at, self.OLD)

    def test_insert_stamps_it(self) -> None:
        t = Tenant(schema_name="beta", company_name="Beta", status=Tenant.Status.NEW,
                   status_changed_at=self.OLD)
        self.assertTrue(t._state.adding)
        self._save(t)
        self.assertGreater(t.status_changed_at, self.OLD)

    def test_update_fields_is_extended(self) -> None:
        """Otherwise the new value is computed and then silently not written — e.g.
        scripts/resolve_cache_bench.py does save(update_fields=["status"])."""
        t = self._loaded(Tenant.Status.ACTIVE)
        t.status = Tenant.Status.DEACTIVATED
        passed = self._save(t, update_fields=["status"])
        self.assertEqual(set(passed["update_fields"]), {"status", "status_changed_at"})

    def test_update_fields_untouched_when_status_unchanged(self) -> None:
        t = self._loaded()
        t.description = "note"
        passed = self._save(t, update_fields=["description"])
        self.assertEqual(list(passed["update_fields"]), ["description"])

    def test_from_db_snapshots_status(self) -> None:
        names = ["id", "schema_name", "company_name", "status"]
        t = Tenant.from_db(None, names, [5, "alpha", "Alpha", Tenant.Status.ACTIVE])
        self.assertEqual(t._loaded_status, Tenant.Status.ACTIVE)

    def test_from_db_tolerates_deferred_status(self) -> None:
        """A .only()/.defer() load must not make from_db refetch the column."""
        t = Tenant.from_db(None, ["id", "schema_name"], [5, "alpha"])
        self.assertFalse(hasattr(t, "_loaded_status"))

    def test_field_is_not_auto_now(self) -> None:
        f = Tenant._meta.get_field("status_changed_at")
        self.assertFalse(f.auto_now)           # see the class docstring: auto_now inverts this
        self.assertFalse(f.auto_now_add)
