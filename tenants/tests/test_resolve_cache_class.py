"""Direct unit tests of TenantResolveCache via DI (inject a fake nx-aware cache).
DB-free: transaction.on_commit is patched to run immediately; Domain is mocked for warm."""
from unittest import mock

from django.test import SimpleTestCase

from tenants.resolve_cache import CacheUnavailable, TenantResolveCache
from tenants.resolve_markers import NEGATIVE, TOMBSTONE

from ._support import FakeNxCache, make_tenant


class _Row:
    def __init__(self, domain, tenant):
        self.domain, self.tenant = domain, tenant


class TenantResolveCacheTests(SimpleTestCase):
    def rc(self, fake=None):
        return TenantResolveCache(cache=fake or FakeNxCache())

    # --- get_snapshot ---
    def test_get_snapshot_absent_is_miss(self):
        rc = self.rc()
        self.assertIs(rc.get_snapshot("h"), rc.MISS)

    def test_get_snapshot_tombstone_is_miss(self):
        fake = FakeNxCache(); fake.store["h"] = TOMBSTONE
        rc = self.rc(fake)
        self.assertIs(rc.get_snapshot("h"), rc.MISS)

    def test_get_snapshot_negative(self):
        fake = FakeNxCache(); fake.store["h"] = NEGATIVE
        rc = self.rc(fake)
        self.assertIs(rc.get_snapshot("h"), rc.NEG)

    def test_get_snapshot_positive_loads_readonly_tenant(self):
        fake = FakeNxCache()
        rc = self.rc(fake)
        fake.store["h"] = rc.dump(make_tenant())
        got = rc.get_snapshot("h")
        self.assertEqual(got.schema_name, "alpha")
        self.assertTrue(got.read_only and got.shard.read_only)

    # --- store / store_miss (nx respects tombstone) ---
    def test_store_is_nx_and_respects_tombstone(self):
        fake = FakeNxCache(); fake.store["h"] = TOMBSTONE
        self.rc(fake).store("h", make_tenant())
        self.assertEqual(fake.store["h"], TOMBSTONE)

    def test_store_miss_writes_negative(self):
        fake = FakeNxCache()
        self.rc(fake).store_miss("h")
        self.assertEqual(fake.store["h"], NEGATIVE)

    # --- invalidation ---
    def test_forget_host_writes_tombstone(self):
        fake = FakeNxCache()
        rc = self.rc(fake)
        with mock.patch("tenants.resolve_cache.transaction.on_commit", side_effect=lambda fn: fn()):
            n = rc.forget_host("h")
        self.assertEqual(n, 1)
        self.assertEqual(fake.store["h"], TOMBSTONE)

    def test_forget_all_clears_by_pattern_and_counts(self):
        fake = FakeNxCache(); fake.store.update({"a": 1, "b": 2})
        n = self.rc(fake).forget_all()
        self.assertEqual(n, 2)
        self.assertEqual(fake.store, {})

    # --- warm (Domain mocked) ---
    def test_warm_fill_gaps_skips_tombstone(self):
        fake = FakeNxCache(); fake.store["h1"] = TOMBSTONE
        rc = self.rc(fake)
        rows = [_Row("h1", make_tenant()), _Row("h2", make_tenant(schema_name="beta"))]
        with mock.patch("tenants.models.Domain") as D:
            D.objects.select_related.return_value.iterator.return_value = iter(rows)
            n = rc.warm()
        self.assertEqual(n, 1)                        # only h2 filled (h1 held by tombstone)
        self.assertEqual(fake.store["h1"], TOMBSTONE)
        self.assertIsInstance(fake.store["h2"], dict)

    def test_warm_force_overwrites_everything(self):
        fake = FakeNxCache(); fake.store["h1"] = TOMBSTONE
        rc = self.rc(fake)
        rows = [_Row("h1", make_tenant()), _Row("h2", make_tenant())]
        with mock.patch("tenants.models.Domain") as D:
            D.objects.select_related.return_value.iterator.return_value = iter(rows)
            n = rc.warm(force=True)
        self.assertEqual(n, 2)
        self.assertIsInstance(fake.store["h1"], dict)  # tombstone overwritten
        self.assertIsInstance(fake.store["h2"], dict)

    # --- health / raise_on_error ---
    def test_raise_on_error_raises_when_down(self):
        rc = self.rc()
        with mock.patch.object(rc, "redis_alive", return_value=False):
            with self.assertRaises(CacheUnavailable):
                rc.forget_all(raise_on_error=True)

    def test_no_raise_when_alive(self):
        rc = self.rc()
        with mock.patch.object(rc, "redis_alive", return_value=True):
            rc.forget_all(raise_on_error=True)         # must not raise

    def test_raise_on_error_honored_for_empty_target(self):
        rc = self.rc()
        with mock.patch.object(rc, "redis_alive", return_value=False):
            with self.assertRaises(CacheUnavailable):
                rc.forget_hosts([], raise_on_error=True)   # empty, still checks alive