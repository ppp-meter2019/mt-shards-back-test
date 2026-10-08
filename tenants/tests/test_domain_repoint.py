"""Domain.save() forgets the OLD host on a re-point. DB-free (no ORM write, on_commit faked).

post_save only ever receives the NEW value, so nothing on the signal path can evict the old
host — it keeps resolving to this tenant for up to POSITIVE_CACHE_SECONDS. TenantSerializer
used to patch that in one caller, which left the admin, the shell and any future command with
a stale host alive. The fix lives on the model for the reason Tenant.save() gives for
status_changed_at: every .save() path stays correct without each caller remembering.
"""
from unittest import mock

from django.test import SimpleTestCase

from tenants.models import Domain


class DomainRepointTests(SimpleTestCase):
    def _save(self, obj, **kw):
        """Run Domain.save() with the DB write and on_commit stubbed out."""
        with mock.patch("django_tenants.models.DomainMixin.save"), \
             mock.patch("django.db.transaction.on_commit", side_effect=lambda fn: fn()), \
             mock.patch("tenants.resolver.resolve_cache.forget_host") as forget:
            obj.save(**kw)
        return [c.args[0] for c in forget.call_args_list]

    def _loaded(self, host):
        """A Domain as it comes back from the DB — from_db takes the snapshot."""
        return Domain.from_db(None, ["id", "domain", "tenant_id", "is_primary"],
                              [1, host, 1, True])

    def test_a_repoint_forgets_the_old_host(self) -> None:
        d = self._loaded("old.example.com")
        d.domain = "new.example.com"
        self.assertEqual(self._save(d), ["old.example.com"])

    def test_an_unrelated_edit_forgets_nothing(self) -> None:
        """is_primary, tenant, anything that is not the hostname."""
        d = self._loaded("acme.example.com")
        d.is_primary = False
        self.assertEqual(self._save(d), [])

    def test_a_fresh_row_forgets_nothing(self) -> None:
        """No snapshot on an unsaved instance — there is no old host to evict."""
        self.assertEqual(self._save(Domain(domain="new.example.com", is_primary=True)), [])

    def test_a_second_repoint_forgets_the_intermediate_host(self) -> None:
        """The snapshot mirrors the column, so the middle value is not stranded. Deleting the
        attribute after the first save instead of refreshing it would leave 'a.example.com'
        resolving to this tenant for an hour."""
        d = self._loaded("old.example.com")
        d.domain = "a.example.com"
        first = self._save(d)
        d.domain = "b.example.com"
        second = self._save(d)
        self.assertEqual(first, ["old.example.com"])
        self.assertEqual(second, ["a.example.com"])

    def test_a_plain_re_save_does_not_forget_twice(self) -> None:
        """Without the mirror, every later save of the same instance would evict the old host
        again — harmless until that host has been re-pointed to ANOTHER tenant, whose valid
        entry would then be thrown away."""
        d = self._loaded("old.example.com")
        d.domain = "new.example.com"
        self._save(d)
        self.assertEqual(self._save(d), [])

    def test_normalization_happens_before_the_comparison(self) -> None:
        """Re-saving with a differently-spelled but equivalent host is NOT a re-point."""
        d = self._loaded("acme.example.com")
        d.domain = "ACME.Example.COM."
        self.assertEqual(self._save(d), [])
        self.assertEqual(d.domain, "acme.example.com")

    def test_a_deferred_load_takes_no_snapshot(self) -> None:
        """.only()/.defer() must not trigger a refetch inside from_db, so there is no snapshot
        and no eviction — the same trade-off Tenant._loaded_status makes."""
        d = Domain.from_db(None, ["id", "is_primary"], [1, True])
        self.assertFalse(hasattr(d, "_loaded_domain"))
