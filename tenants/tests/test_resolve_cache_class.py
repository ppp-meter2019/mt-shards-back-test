"""Direct unit tests of TenantResolveCache via DI (inject a fake nx-aware cache).
DB-free: transaction.on_commit is patched to run immediately; Domain is mocked for warm."""
from unittest import mock

from django.test import SimpleTestCase

from tenants.resolver import CacheUnavailable, TenantResolveCache
from tenants.resolver import NEGATIVE, TOMBSTONE

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
        rc = self.rc()
        rc.cache.store[rc._snap_key("h")] = TOMBSTONE
        self.assertIs(rc.get_snapshot("h"), rc.MISS)

    def test_get_snapshot_negative(self):
        rc = self.rc()
        rc.cache.store[rc._snap_key("h")] = NEGATIVE
        self.assertIs(rc.get_snapshot("h"), rc.NEG)

    def test_get_snapshot_positive_loads_readonly_tenant(self):
        rc = self.rc()
        rc.cache.store[rc._snap_key("h")] = rc.dump(make_tenant())
        got = rc.get_snapshot("h")
        self.assertEqual(got.schema_name, "alpha")
        self.assertTrue(got.read_only and got.shard.read_only)

    def test_classify_covers_all_value_kinds(self):
        rc = self.rc()
        K = rc._Kind
        self.assertIs(rc._classify(None), K.MISS)
        self.assertIs(rc._classify(TOMBSTONE), K.HOLD)
        self.assertIs(rc._classify(NEGATIVE), K.NEG)
        self.assertIs(rc._classify(rc.dump(make_tenant())), K.POSITIVE)
        self.assertIs(rc._classify({"tenant": {}, "shard": {}}), K.POSITIVE)  # right shape
        self.assertIs(rc._classify({"foo": 1}), K.UNKNOWN)                    # malformed dict
        self.assertIs(rc._classify("garbage"), K.UNKNOWN)                     # non-dict

    def test_get_snapshot_malformed_dict_is_miss_not_raise(self):
        rc = self.rc()
        rc.cache.store[rc._snap_key("h")] = {"foo": 1}   # dict but wrong shape
        self.assertIs(rc.get_snapshot("h"), rc.MISS)     # UNKNOWN → MISS, no load()/KeyError

    def test_dump_allowlist_excludes_non_routing_fields(self):
        # Snapshot carries ONLY the routing allowlist — no company_name/last_error/etc.
        rc = self.rc()
        snap = rc.dump(make_tenant())
        self.assertEqual(set(snap["tenant"]), {"id", "schema_name", "status", "shard_id"})
        self.assertEqual(set(snap["shard"]), {"id", "alias"})
        # routing fields round-trip; a non-carried field is the model default, never read
        # off request.tenant (see the _SNAPSHOT_FIELDS contract).
        got = rc.load(snap)
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(got.shard.alias, "shard_a")
        self.assertEqual(got.company_name, "")      # make_tenant set "Alpha" — NOT carried

    # --- store / store_miss (nx respects tombstone) ---
    def test_store_is_nx_and_respects_tombstone(self):
        rc = self.rc()
        k = rc._snap_key("h")
        rc.cache.store[k] = TOMBSTONE
        rc.store("h", make_tenant())
        self.assertEqual(rc.cache.store[k], TOMBSTONE)

    def test_store_miss_writes_negative(self):
        rc = self.rc()
        rc.store_miss("h")
        self.assertEqual(rc.cache.store[rc._snap_key("h")], NEGATIVE)

    # --- invalidation ---
    def test_forget_host_writes_tombstone(self):
        rc = self.rc()
        with mock.patch("tenants.resolver.cache.transaction.on_commit", side_effect=lambda fn: fn()):
            n = rc.forget_host("h")
        self.assertEqual(n, 1)
        self.assertEqual(rc.cache.store[rc._snap_key("h")], TOMBSTONE)

    def test_forget_all_clears_by_pattern_and_counts(self):
        fake = FakeNxCache(); fake.store.update({"a": 1, "b": 2})
        n = self.rc(fake).forget_all()
        self.assertEqual(n, 2)
        self.assertEqual(fake.store, {})

    # --- warm (Domain mocked) ---
    def test_warm_fill_gaps_skips_tombstone(self):
        rc = self.rc()
        rc.cache.store[rc._snap_key("h1")] = TOMBSTONE
        rows = [_Row("h1", make_tenant()), _Row("h2", make_tenant(schema_name="beta"))]
        with mock.patch("tenants.models.Domain") as D:
            D.objects.select_related.return_value.iterator.return_value = iter(rows)
            n = rc.warm()
        self.assertEqual(n, 1)                        # only h2 filled (h1 held by tombstone)
        self.assertEqual(rc.cache.store[rc._snap_key("h1")], TOMBSTONE)
        self.assertIsInstance(rc.cache.store[rc._snap_key("h2")], dict)

    def test_warm_force_overwrites_everything(self):
        rc = self.rc()
        rc.cache.store[rc._snap_key("h1")] = TOMBSTONE
        rows = [_Row("h1", make_tenant()), _Row("h2", make_tenant())]
        with mock.patch("tenants.models.Domain") as D:
            D.objects.select_related.return_value.iterator.return_value = iter(rows)
            n = rc.warm(force=True)
        self.assertEqual(n, 2)
        self.assertIsInstance(rc.cache.store[rc._snap_key("h1")], dict)  # tombstone overwritten
        self.assertIsInstance(rc.cache.store[rc._snap_key("h2")], dict)

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