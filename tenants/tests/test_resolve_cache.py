"""Resolution cache via the middleware: hit/miss/negative, nx+tombstone race,
fail-open, uniform read-only, dump/load fidelity. DB-free (fake domain model +
fake nx-aware cache injected as a TenantResolveCache)."""
from unittest import mock

from django.test import SimpleTestCase

import tenants.middleware as mw
from tenants.models import ReadOnlyInstanceError
from tenants.resolver import TenantResolveCache
from tenants.resolver import NEGATIVE, TOMBSTONE

from ._support import FakeNxCache, make_domain_model, make_tenant, use_resolve_cache


class ResolveCacheTests(SimpleTestCase):
    def setUp(self):
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)

    def test_miss_then_hit_hits_db_once(self):
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()):
            self.mw.get_tenant(dm, "known")
            self.mw.get_tenant(dm, "known")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_negative_cached_no_second_db(self):
        dm = make_domain_model(None)
        with use_resolve_cache(FakeNxCache()):
            for _ in range(2):
                with self.assertRaises(dm.DoesNotExist):
                    self.mw.get_tenant(dm, "nope")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_request_tenant_read_only_on_miss_and_hit(self):
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()):
            miss = self.mw.get_tenant(dm, "known")
            hit = self.mw.get_tenant(dm, "known")
        for got in (miss, hit):
            self.assertTrue(got.read_only)
            self.assertTrue(got.shard.read_only)
            with self.assertRaises(ReadOnlyInstanceError):
                got.save()

    def test_tombstone_blocks_stale_nx_write(self):
        fake = FakeNxCache()
        fake.store["known"] = TOMBSTONE
        self.assertFalse(fake.set("known", {"stale": 1}, 60, nx=True))
        self.assertEqual(fake.store["known"], TOMBSTONE)

    def test_tombstone_is_treated_as_miss_db_direct(self):
        dm = make_domain_model(make_tenant())
        fake = FakeNxCache()
        fake.store["known"] = TOMBSTONE
        with use_resolve_cache(fake):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(dm.db_calls["n"], 1)
        self.assertEqual(fake.store["known"], TOMBSTONE)

    def test_fail_open_on_backend_without_nx(self):
        class NoNxCache:
            def get(self, key, default=None):
                return None
            def set(self, *a, **k):
                if "nx" in k:
                    raise TypeError("set() got an unexpected keyword argument 'nx'")
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(NoNxCache()):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")

    def test_fail_open_on_corrupt_entry(self):
        fake = FakeNxCache()
        fake.store["known"] = {"garbage": 1}
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(fake):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")

    def test_does_not_exist_propagates(self):
        dm = make_domain_model(None)
        with use_resolve_cache(FakeNxCache()):
            with self.assertRaises(dm.DoesNotExist):
                self.mw.get_tenant(dm, "nope")

    def test_raw_psycopg_op_error_normalized_not_retried(self):
        # django-tenants runs `SET search_path` on a raw psycopg cursor, so a pool/proxy
        # timeout escapes as a raw psycopg.OperationalError (NOT django.db.OperationalError).
        # It must be surfaced as a DB outage — normalized to django OperationalError, NOT
        # mislabeled a cache failure and retried against a dead DB.
        import psycopg
        from django.db import OperationalError

        calls = {"n": 0}

        class _Objects:
            @classmethod
            def select_related(cls, *a):
                return cls

            @classmethod
            def get(cls, domain=None):
                calls["n"] += 1
                raise psycopg.OperationalError("Timed-out waiting to acquire database connection.")

        class FakeDomain:
            DoesNotExist = type("DoesNotExist", (Exception,), {})
            objects = _Objects

        with use_resolve_cache(FakeNxCache()):
            with self.assertRaises(OperationalError):        # normalized, not raw psycopg
                self.mw.get_tenant(FakeDomain, "known")
        self.assertEqual(calls["n"], 1)                      # surfaced once, NOT retried

    def test_dump_load_round_trip_fidelity(self):
        r = TenantResolveCache.load(TenantResolveCache.dump(make_tenant()))
        self.assertEqual(r.schema_name, "alpha")
        self.assertEqual(r.shard.alias, "shard_a")
        self.assertEqual(r.shard_id, 2)
        self.assertTrue(r.read_only and r.shard.read_only)


class SweepOrphansTests(SimpleTestCase):
    class _Cache:
        """Batch-aware fake: sweep_orphans reads via get_many and deletes via delete_many."""

        def __init__(self, store):
            self.store = store
            self.deleted = []

        def get_many(self, keys):
            return {k: self.store[k] for k in keys if k in self.store}

        def delete_many(self, keys):
            for k in keys:
                self.deleted.append(k)
                self.store.pop(k, None)

    def test_deletes_only_orphan_positives(self):
        rc = TenantResolveCache(cache=None)
        K = rc._snap_key
        rc._cache = self._Cache({
            K("keep.com"): {"tenant": {}, "shard": {}},   # valid positive → keep
            K("gone.com"): {"tenant": {}, "shard": {}},   # ORPHAN positive → delete
            K("neg.com"):  NEGATIVE,                       # cached miss (not a dict) → leave
            K("hold.com"): TOMBSTONE,                      # hold marker → leave
        })
        with mock.patch.object(rc, "iter_snapshot_hosts",
                               return_value=iter(["keep.com", "gone.com", "neg.com", "hold.com"])):
            n = rc.sweep_orphans(valid_hosts={"keep.com"})
        self.assertEqual(n, 1)
        self.assertEqual(rc.cache.deleted, [K("gone.com")])   # only the orphan POSITIVE
        for kept in ("keep.com", "neg.com", "hold.com"):
            self.assertIn(K(kept), rc.cache.store)