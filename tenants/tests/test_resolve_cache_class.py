"""Direct unit tests of TenantResolveCache via DI (inject a fake nx-aware cache).
DB-free: transaction.on_commit is patched to run immediately; Domain is mocked for warm."""
from unittest import mock

from django.test import SimpleTestCase, override_settings

from tenants.resolver import CacheUnavailable, LegacyWarmRefused, TenantResolveCache
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

    def test_get_snapshot_positive_loads_a_snapshot(self):
        from tenants.resolver import TenantSnapshot
        rc = self.rc()
        rc.cache.store[rc._snap_key("h")] = rc.dump(make_tenant())
        got = rc.get_snapshot("h")
        self.assertIsInstance(got, TenantSnapshot)
        self.assertEqual(got.schema_name, "alpha")
        self.assertFalse(hasattr(got, "save"))

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
        # The payload carries ONLY the routing allowlist — no company_name/last_error/etc.
        # No `shard_id` either: it duplicated shard.id and existed only to keep a rebuilt
        # MODEL instance self-consistent, which load() no longer produces.
        rc = self.rc()
        snap = rc.dump(make_tenant())
        self.assertEqual(set(snap["tenant"]), {"id", "schema_name", "status"})
        self.assertEqual(set(snap["shard"]), {"id", "alias"})
        got = rc.load(snap)
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(got.shard.alias, "shard_a")
        # make_tenant set company_name="Alpha"; it is ABSENT rather than defaulted to ""
        # — that is the difference between an AttributeError and a plausible wrong value.
        self.assertFalse(hasattr(got, "company_name"))

    # --- store / store_miss (nx respects tombstone) ---
    def test_store_is_nx_and_respects_tombstone(self):
        rc = self.rc()
        k = rc._snap_key("h")
        rc.cache.store[k] = TOMBSTONE
        rc.put("h", make_tenant())
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

    # --- schema-snap lockstep (invalidation) ---
    def _run_on_commit(self):
        return mock.patch("tenants.resolver.cache.transaction.on_commit",
                          side_effect=lambda fn: fn())

    def test_keys_for_key_names(self):
        rc = self.rc()
        self.assertEqual(rc._schema_snap_key("alpha"), "schema-snap:alpha")

    def test_forget_hosts_derives_schema_from_cached_snapshot(self):
        # A warm host snapshot carries schema_name -> forget_hosts drops BOTH namespaces.
        rc = self.rc()
        rc.cache.store[rc._snap_key("h")] = rc.dump(make_tenant(schema_name="alpha"))
        with self._run_on_commit():
            rc.forget_hosts(["h"])
        self.assertEqual(rc.cache.store[rc._snap_key("h")], TOMBSTONE)
        self.assertEqual(rc.cache.store[rc._schema_snap_key("alpha")], TOMBSTONE)

    def test_forget_hosts_explicit_schema_without_host(self):
        # The post_delete path: no host, explicit schema -> only the schema-snap is dropped.
        rc = self.rc()
        with self._run_on_commit():
            n = rc.forget_hosts([], schemas={"beta"})
        self.assertEqual(n, 1)
        self.assertEqual(rc.cache.store[rc._schema_snap_key("beta")], TOMBSTONE)

    def test_forget_hosts_cold_snapshot_leaves_schema_to_backstop(self):
        # No cached host snapshot -> schema can't be derived -> only the host key is tombstoned
        # (the Tenant post_delete receiver / reconcile sweep is the reliable schema cleaner).
        rc = self.rc()
        with self._run_on_commit():
            n = rc.forget_hosts(["h"])
        self.assertEqual(n, 1)
        self.assertEqual(rc.cache.store[rc._snap_key("h")], TOMBSTONE)
        self.assertEqual([k for k in rc.cache.store if k.startswith("schema-snap:")], [])

    # --- schema-snap read/write (worker cache primitives) ---
    def test_get_schema_snapshot_positive_hold_miss(self):
        rc = self.rc()
        self.assertIs(rc.get_schema_snapshot("s"), rc.MISS)                    # absent
        rc.cache.store[rc._schema_snap_key("s")] = TOMBSTONE
        self.assertIs(rc.get_schema_snapshot("s"), rc.HOLD)                    # active invalidation
        rc.cache.store[rc._schema_snap_key("s")] = rc.dump(make_tenant(schema_name="s"))
        got = rc.get_schema_snapshot("s")
        self.assertEqual(got.schema_name, "s")
        self.assertFalse(hasattr(got, "save"))

    @override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
    def test_put_warms_both_host_and_schema(self):
        # The FRONT resolve fill (put/store) warms BOTH namespaces in lockstep, so a front
        # request re-warms the worker's schema-snap too (the worker never writes the cache).
        rc = self.rc()
        rc.put("acme.com", make_tenant(schema_name="acme"))
        self.assertIsInstance(rc.cache.store[rc._snap_key("acme.com")], dict)
        self.assertIsInstance(rc.cache.store[rc._schema_snap_key("acme")], dict)

    @override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
    def test_put_is_nx_and_respects_hold_on_schema(self):
        rc = self.rc()
        rc.cache.store[rc._schema_snap_key("acme")] = TOMBSTONE          # schema held (invalidated)
        rc.put("acme.com", make_tenant(schema_name="acme"))
        self.assertEqual(rc.cache.store[rc._schema_snap_key("acme")], TOMBSTONE)  # nx respected hold
        self.assertIsInstance(rc.cache.store[rc._snap_key("acme.com")], dict)     # host still filled

    @override_settings(TENANT_REGISTRY={"WARM_ENABLED": True},
                       TENANT_RESOLVE={"WARM_TTL_BY_STATUS": {"active": None}})
    def test_put_schema_many_writes_each_schema_key(self):
        rc = self.rc()
        n = rc.put_schema_many([make_tenant(schema_name="s1"), make_tenant(schema_name="s2")])
        self.assertEqual(n, 2)
        self.assertIn(rc._schema_snap_key("s1"), rc.cache.store)
        self.assertIn(rc._schema_snap_key("s2"), rc.cache.store)

    def test_forget_all_clears_by_pattern_and_counts(self):
        fake = FakeNxCache(); fake.store.update({"a": 1, "b": 2})
        n = self.rc(fake).forget_all()
        self.assertEqual(n, 2)
        self.assertEqual(fake.store, {})

    # --- put: return contract ---
    def test_put_reports_whether_the_host_snap_was_written(self):
        """put() returns the nx OUTCOME, not "caching is enabled": a held tombstone means the
        write did not land, and the caller must be able to tell."""
        rc = self.rc()
        self.assertTrue(rc.put("h", make_tenant()))              # fresh key -> written
        self.assertFalse(rc.put("h", make_tenant()))             # nx declined -> already there
        rc.cache.store[rc._snap_key("held")] = TOMBSTONE
        self.assertFalse(rc.put("held", make_tenant()))          # tombstone respected

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 0, "MISS_CACHE_SECONDS": 60})
    def test_put_is_false_when_positive_caching_is_off(self):
        self.assertFalse(self.rc().put("h", make_tenant()))

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

    def test_warm_writes_schema_snaps_deduped_per_tenant(self):
        """warm() must keep BOTH namespaces in lockstep, like put() and reconcile: host-only
        warming left the WORKER cache (get_schema_snapshot) cold after a 'successful' warm.
        One schema-snap per DISTINCT tenant — two domains of one tenant must not cost two."""
        rc = self.rc()
        alpha, beta = make_tenant(schema_name="alpha"), make_tenant(schema_name="beta")
        rows = [_Row("a1.com", alpha), _Row("a2.com", alpha), _Row("b.com", beta)]
        with mock.patch("tenants.models.Domain") as D:
            D.objects.select_related.return_value.iterator.return_value = iter(rows)
            n = rc.warm(force=True)
        self.assertEqual(n, 3)                                   # counts DOMAINS warmed
        self.assertEqual(
            sorted(k for k in rc.cache.store if k.startswith("schema-snap:")),
            ["schema-snap:alpha", "schema-snap:beta"],
        )

    def test_warm_nx_writes_schema_snaps_too(self):
        rc = self.rc()
        rows = [_Row("a.com", make_tenant(schema_name="alpha"))]
        with mock.patch("tenants.models.Domain") as D:
            D.objects.select_related.return_value.iterator.return_value = iter(rows)
            rc.warm()
        self.assertIn("schema-snap:alpha", rc.cache.store)

    # --- warm: the WARM-stage guard ---
    @override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
    def test_warm_refuses_under_warm_stage(self):
        """warm() writes positives WITHOUT building treg:hosts, so under WARM it must refuse
        rather than silently half-warm the cache. Both branches (nx and force) are covered:
        neither builds the SET."""
        rc = self.rc()
        for kwargs in ({}, {"force": True}):
            with self.subTest(**kwargs), self.assertRaises(LegacyWarmRefused):
                rc.warm(**kwargs)

    @override_settings(TENANT_REGISTRY={"WARM_ENABLED": True},
                       TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 0})
    def test_warm_guard_precedes_the_positive_ttl_early_return(self):
        """WARM on + flat TTL disabled is a VALID config (see the `enabled` property) and the
        worst one to get wrong: if the guard sat after the `_pos_ttl` early-return, this call
        would swallow the misuse as a silent `return 0`."""
        with self.assertRaises(LegacyWarmRefused):
            self.rc().warm()

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


class TenantDeleteSignalTests(SimpleTestCase):
    """The Tenant post_delete receiver drops the schema-snap deterministically by schema_name
    (the domains — and their host-snaps — are already gone via the cascade)."""

    def test_post_delete_drops_schema_snapshot(self):
        from tenants import signals
        inst = mock.Mock(schema_name="gamma")
        with mock.patch.object(signals.resolve_cache, "forget_schemas") as fs:
            signals.invalidate_tenant_deleted(sender=None, instance=inst)
        fs.assert_called_once_with(["gamma"])