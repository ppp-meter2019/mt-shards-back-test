"""Resolution cache: hit/miss/negative, nx+tombstone race, fail-open, uniform
read-only, dump/load fidelity. DB-free (fake domain model + fake nx-aware cache)."""
from django.test import SimpleTestCase

import tenants.middleware as mw
import tenants.resolve_cache as rc
from tenants.models import ReadOnlyInstanceError

from ._support import FakeNxCache, make_domain_model, make_tenant, use_cache


class ResolveCacheTests(SimpleTestCase):
    def setUp(self):
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)

    def test_miss_then_hit_hits_db_once(self):
        dm = make_domain_model(make_tenant())
        with use_cache(FakeNxCache(), mw):
            self.mw.get_tenant(dm, "known")
            self.mw.get_tenant(dm, "known")
        self.assertEqual(dm.db_calls["n"], 1)   # 2nd served from cache

    def test_negative_cached_no_second_db(self):
        dm = make_domain_model(None)
        with use_cache(FakeNxCache(), mw):
            for _ in range(2):
                with self.assertRaises(dm.DoesNotExist):
                    self.mw.get_tenant(dm, "nope")
        self.assertEqual(dm.db_calls["n"], 1)   # 2nd served from negative cache

    def test_request_tenant_read_only_on_miss_and_hit(self):
        dm = make_domain_model(make_tenant())
        with use_cache(FakeNxCache(), mw):
            miss = self.mw.get_tenant(dm, "known")
            hit = self.mw.get_tenant(dm, "known")
        for got in (miss, hit):
            self.assertTrue(got.read_only)
            self.assertTrue(got.shard.read_only)
            with self.assertRaises(ReadOnlyInstanceError):
                got.save()

    def test_tombstone_blocks_stale_nx_write(self):
        fake = FakeNxCache()
        fake.store["known"] = rc.TOMBSTONE                      # post-invalidation hold
        wrote = fake.set("known", {"stale": 1}, 60, nx=True)    # slow resolver tries to write stale
        self.assertFalse(wrote)
        self.assertEqual(fake.store["known"], rc.TOMBSTONE)     # stale not written

    def test_tombstone_is_treated_as_miss_db_direct(self):
        dm = make_domain_model(make_tenant())
        fake = FakeNxCache()
        fake.store["known"] = rc.TOMBSTONE
        with use_cache(fake, mw):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")              # resolved fresh
        self.assertEqual(dm.db_calls["n"], 1)                   # DB-direct during hold
        self.assertEqual(fake.store["known"], rc.TOMBSTONE)     # nx did not overwrite the hold

    def test_fail_open_on_backend_without_nx(self):
        class NoNxCache:
            def get(self, key, default=None):
                return None
            def set(self, *a, **k):
                if "nx" in k:
                    raise TypeError("set() got an unexpected keyword argument 'nx'")
        dm = make_domain_model(make_tenant())
        with use_cache(NoNxCache(), mw):
            got = self.mw.get_tenant(dm, "known")               # TypeError -> DB fallback
        self.assertEqual(got.schema_name, "alpha")

    def test_fail_open_on_corrupt_entry(self):
        fake = FakeNxCache()
        fake.store["known"] = {"garbage": 1}                    # not tombstone/negative -> _load KeyError
        dm = make_domain_model(make_tenant())
        with use_cache(fake, mw):
            got = self.mw.get_tenant(dm, "known")               # _load fails -> DB fallback
        self.assertEqual(got.schema_name, "alpha")

    def test_does_not_exist_propagates_not_swallowed(self):
        dm = make_domain_model(None)
        with use_cache(FakeNxCache(), mw):
            with self.assertRaises(dm.DoesNotExist):
                self.mw.get_tenant(dm, "nope")

    def test_dump_load_round_trip_fidelity(self):
        r = mw._load(mw._dump(make_tenant()))
        self.assertEqual(r.schema_name, "alpha")
        self.assertEqual(r.shard.alias, "shard_a")
        self.assertEqual(r.shard_id, 2)
        self.assertTrue(r.read_only and r.shard.read_only)