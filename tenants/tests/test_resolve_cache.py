"""Resolution cache via the middleware: hit/miss/negative, nx+tombstone race,
fail-open, uniform read-only, dump/load fidelity. DB-free (fake domain model +
fake nx-aware cache injected as a TenantResolveCache)."""
from collections.abc import Iterable
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

import tenants.middleware as mw
from tenants.resolver import TenantResolveCache
from tenants.resolver import NEGATIVE, TOMBSTONE

from ._support import FakeNxCache, make_domain_model, make_tenant, use_resolve_cache


class ResolveCacheTests(SimpleTestCase):
    def setUp(self) -> None:
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)

    def test_miss_then_hit_hits_db_once(self) -> None:
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()):
            self.mw.get_tenant(dm, "known")
            self.mw.get_tenant(dm, "known")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_negative_cached_no_second_db(self) -> None:
        dm = make_domain_model(None)
        with use_resolve_cache(FakeNxCache()):
            for _ in range(2):
                with self.assertRaises(dm.DoesNotExist):
                    self.mw.get_tenant(dm, "nope")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_miss_and_hit_expose_exactly_the_same_thing(self) -> None:
        """The invariant the snapshot type exists for. The DB resolve used to hand out the
        full row while the cache handed out a partial model instance, so a field like
        company_name read correctly on a cold cache and as "" on a warm one."""
        from tenants.resolver import TenantSnapshot
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()):
            miss = self.mw.get_tenant(dm, "known")
            hit = self.mw.get_tenant(dm, "known")
        self.assertIsInstance(miss, TenantSnapshot)
        self.assertIsInstance(hit, TenantSnapshot)
        self.assertEqual(miss, hit)                       # field-for-field
        for got in (miss, hit):
            self.assertFalse(hasattr(got, "save"))
            self.assertFalse(hasattr(got, "company_name"))

    def test_tombstone_blocks_stale_nx_write(self) -> None:
        fake = FakeNxCache()
        fake.store["known"] = TOMBSTONE
        self.assertFalse(fake.set("known", {"stale": 1}, 60, nx=True))
        self.assertEqual(fake.store["known"], TOMBSTONE)

    def test_tombstone_is_treated_as_miss_db_direct(self) -> None:
        dm = make_domain_model(make_tenant())
        fake = FakeNxCache()
        fake.store["known"] = TOMBSTONE
        with use_resolve_cache(fake):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(dm.db_calls["n"], 1)
        self.assertEqual(fake.store["known"], TOMBSTONE)

    def test_fail_open_on_backend_without_nx(self) -> None:
        class NoNxCache:
            def get(self, key: str, default: Any = None) -> Any:
                return None
            def set(self, *a: Any, **k: Any) -> None:
                if "nx" in k:
                    raise TypeError("set() got an unexpected keyword argument 'nx'")
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(NoNxCache()):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")

    def test_fail_open_on_corrupt_entry(self) -> None:
        fake = FakeNxCache()
        fake.store["known"] = {"garbage": 1}
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(fake):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")

    def test_does_not_exist_propagates(self) -> None:
        dm = make_domain_model(None)
        with use_resolve_cache(FakeNxCache()):
            with self.assertRaises(dm.DoesNotExist):
                self.mw.get_tenant(dm, "nope")

    def test_raw_psycopg_op_error_normalized_not_retried(self) -> None:
        # django-tenants runs `SET search_path` on a raw psycopg cursor, so a pool/proxy
        # timeout escapes as a raw psycopg.OperationalError (NOT django.db.OperationalError).
        # It must be surfaced as a DB outage — normalized to django OperationalError, NOT
        # mislabeled a cache failure and retried against a dead DB.
        import psycopg
        from django.db import OperationalError

        calls = {"n": 0}

        class _Objects:
            @classmethod
            def select_related(cls, *a: Any) -> type:
                return cls

            @classmethod
            def get(cls, domain: str | None = None) -> None:
                calls["n"] += 1
                raise psycopg.OperationalError("Timed-out waiting to acquire database connection.")

        class FakeDomain:
            DoesNotExist = type("DoesNotExist", (Exception,), {})
            objects = _Objects

        with use_resolve_cache(FakeNxCache()):
            with self.assertRaises(OperationalError):        # normalized, not raw psycopg
                self.mw.get_tenant(FakeDomain, "known")
        self.assertEqual(calls["n"], 1)                      # surfaced once, NOT retried

    def test_raw_psycopg_interface_error_normalized_not_retried(self) -> None:
        # A closed/broken raw-cursor connection escapes as psycopg.InterfaceError — a SIBLING
        # of psycopg.OperationalError, not a subclass — so it must be normalized on its own.
        # Same contract: surfaced as a django DB outage, once, not retried.
        import psycopg
        from django.db import OperationalError

        calls = {"n": 0}

        class _Objects:
            @classmethod
            def select_related(cls, *a: Any) -> type:
                return cls

            @classmethod
            def get(cls, domain: str | None = None) -> None:
                calls["n"] += 1
                raise psycopg.InterfaceError("connection already closed")

        class FakeDomain:
            DoesNotExist = type("DoesNotExist", (Exception,), {})
            objects = _Objects

        with use_resolve_cache(FakeNxCache()):
            with self.assertRaises(OperationalError):        # normalized, not raw psycopg
                self.mw.get_tenant(FakeDomain, "known")
        self.assertEqual(calls["n"], 1)                      # surfaced once, NOT retried

    def test_dump_load_round_trip_fidelity(self) -> None:
        from tenants.resolver import TenantSnapshot
        t = make_tenant()
        r = TenantResolveCache.load(TenantResolveCache.dump(t))
        self.assertEqual(r.schema_name, "alpha")
        self.assertEqual(r.shard.alias, "shard_a")
        self.assertEqual(r.shard.id, 2)
        # The round trip and the direct capture must land on the same value — that is what
        # makes a hit indistinguishable from a miss.
        self.assertEqual(r, TenantSnapshot.capture(t))

    def test_dump_accepts_a_snapshot_as_well_as_a_model(self) -> None:
        """The resolve path hands dump() an already-captured snapshot; reconcile and warm
        hand it Domain.tenant rows. Both must produce the same payload."""
        from tenants.resolver import TenantSnapshot
        t = make_tenant()
        self.assertEqual(TenantResolveCache.dump(t),
                         TenantResolveCache.dump(TenantSnapshot.capture(t)))

    def test_payload_never_carries_the_request_hostname(self) -> None:
        """domain_url is a declared field (django_tenants writes it) but is per-REQUEST —
        caching it would stamp one hostname onto a snapshot shared by every domain of the
        tenant."""
        snap = TenantResolveCache.load(TenantResolveCache.dump(make_tenant()))
        self.assertIsNone(snap.domain_url)
        payload = TenantResolveCache.dump(make_tenant())
        self.assertNotIn("domain_url", payload["tenant"])


class SweepOrphansTests(SimpleTestCase):
    class _Cache:
        """Batch-aware fake: sweep_orphans reads via get_many and deletes via delete_many."""

        def __init__(self, store: dict[str, Any]) -> None:
            self.store = store
            self.deleted = []

        def get_many(self, keys: Iterable[str]) -> dict[str, Any]:
            return {k: self.store[k] for k in keys if k in self.store}

        def delete_many(self, keys: Iterable[str]) -> None:
            for k in keys:
                self.deleted.append(k)
                self.store.pop(k, None)

    def test_deletes_only_orphan_positives(self) -> None:
        rc = TenantResolveCache(cache=None)
        K = rc._snap_key
        rc._cache = self._Cache({
            K("keep.com"): {"tenant": {}, "shard": {}},   # valid positive → keep
            K("gone.com"): {"tenant": {}, "shard": {}},   # ORPHAN positive → delete
            K("neg.com"):  NEGATIVE,                       # cached miss (not a dict) → leave
            K("hold.com"): TOMBSTONE,                      # hold marker → leave
        })
        with mock.patch.object(rc, "iter_snapshot_hosts",
                               return_value=iter(["keep.com", "gone.com", "neg.com", "hold.com"])), \
             mock.patch.object(rc, "iter_snapshot_schemas", return_value=iter([])):
            n = rc.sweep_orphans(valid_hosts={"keep.com"})
        self.assertEqual(n, 1)
        self.assertEqual(rc.cache.deleted, [K("gone.com")])   # only the orphan POSITIVE
        for kept in ("keep.com", "neg.com", "hold.com"):
            self.assertIn(K(kept), rc.cache.store)