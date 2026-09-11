"""Direct unit tests of the reconcile WRITE side — HostRegistry.reconcile / _rebuild_once —
plus the ttl_for_status policy they depend on. DB-free: Domain/Tenant are mocked and the raw
Redis client is a fake (tests._support.FakeSetRedis).

These cover what RunLockedFencingTests (test_resolve_gate.py) deliberately does NOT: that
suite mocks `reconcile` OUT so it can test the lock protocol AROUND the rebuild. The rebuild
itself — the write side the whole GATE stage rests on — is covered only here.

Design: deploy/resolve_gate_design.md.
"""
from typing import Any
from unittest import mock

from django.test import SimpleTestCase, override_settings

from tenants.models import Tenant
from tenants.resolver import (
    DIRTY_KEY,
    HOSTS_KEY,
    HOSTS_NEW_KEY,
    HostRegistry,
    host_registry,
    resolve_cache,
)
from tenants.resolver.config import RESOLVE_DEFAULTS

from ._support import FakeNxCache, FakeSetRedis, make_tenant


class _Row:
    """A Domain row as _rebuild_once consumes it: .domain + .tenant (with .shard)."""

    def __init__(self, domain: str, tenant: Any) -> None:
        self.domain, self.tenant = domain, tenant


def _rows(*pairs: tuple[str, Any]) -> list[Any]:
    return [_Row(host, tenant) for host, tenant in pairs]


@override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
class RebuildOnceTests(SimpleTestCase):
    """_rebuild_once: build into treg:hosts:new, publish with an atomic RENAME — or DELETE
    treg:hosts when the DB has no domains at all (flag absent => gate fails open)."""

    def setUp(self) -> None:
        # resolve_cache is a module-level singleton that registry.py imported by value, so
        # DI'ing its cache here is what put_many / put_schema_many will write through.
        self.fake_cache = FakeNxCache()
        patcher = mock.patch.object(resolve_cache, "_cache", self.fake_cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, rows: Any, redis: Any = None, chunk: int | None = None) -> tuple[Any, ...]:
        c = redis or FakeSetRedis()
        with mock.patch("tenants.models.Domain") as D:
            D.objects.select_related.return_value.iterator.return_value = iter(rows)
            if chunk is None:
                n, hosts = HostRegistry._rebuild_once(c)
            else:
                with mock.patch.object(HostRegistry, "_REBUILD_CHUNK", chunk):
                    n, hosts = HostRegistry._rebuild_once(c)
        return c, n, hosts

    def test_nonempty_db_renames_new_over_hosts(self) -> None:
        t = make_tenant(schema_name="alpha")
        c, n, hosts = self._run(_rows(("a.com", t), ("b.com", t)))
        self.assertEqual(hosts, {"a.com", "b.com"})
        self.assertEqual(n, 2)                                   # host-snaps written
        self.assertEqual(c.keys[HOSTS_KEY], {"a.com", "b.com"})
        self.assertNotIn(HOSTS_NEW_KEY, c.keys)                  # consumed by the rename
        self.assertIn(("rename", HOSTS_NEW_KEY, HOSTS_KEY), c.ops)

    def test_build_happens_in_the_new_key_before_the_swap(self) -> None:
        """Ordering IS the invariant: nothing may SADD into treg:hosts directly, or a
        half-built SET becomes authoritative and the gate rejects live hosts."""
        c, _, _ = self._run(_rows(("a.com", make_tenant())))
        sadds = [op for op in c.ops if op[0] == "sadd"]
        self.assertTrue(sadds)
        self.assertTrue(all(op[1] == HOSTS_NEW_KEY for op in sadds))
        self.assertLess(c.ops.index(sadds[-1]),
                        c.ops.index(("rename", HOSTS_NEW_KEY, HOSTS_KEY)))

    def test_empty_db_deletes_hosts_so_the_gate_fails_open(self) -> None:
        """No domains => the flag key must be ABSENT (check() -> UNKNOWN -> fail-open), NOT
        an empty SET, which would be present and would reject every host."""
        c = FakeSetRedis()
        c.keys[HOSTS_KEY] = {"stale.com"}          # a previous generation
        c, n, hosts = self._run([], redis=c)
        self.assertEqual((n, hosts), (0, set()))
        self.assertNotIn(HOSTS_KEY, c.keys)
        self.assertIn(("delete", HOSTS_KEY), c.ops)

    def test_stale_new_key_is_cleared_before_building(self) -> None:
        """A crash mid-rebuild leaves treg:hosts:new behind; it must not leak into the next
        generation's SET."""
        c = FakeSetRedis()
        c.keys[HOSTS_NEW_KEY] = {"leftover.com"}
        c, _, _ = self._run(_rows(("a.com", make_tenant())), redis=c)
        self.assertEqual(c.keys[HOSTS_KEY], {"a.com"})
        self.assertEqual(c.ops[0], ("delete", HOSTS_NEW_KEY))    # cleared FIRST

    def test_chunking_flushes_per_chunk(self) -> None:
        """put_many materializes every payload it is handed, so _rebuild_once must bound
        memory by flushing per chunk rather than accumulating the whole table."""
        t = make_tenant()
        c, n, hosts = self._run(_rows(("a.com", t), ("b.com", t), ("c.com", t)), chunk=2)
        self.assertEqual(n, 3)
        self.assertEqual(len([op for op in c.ops if op[0] == "sadd"]), 2)   # 2 + 1
        self.assertEqual(c.keys[HOSTS_KEY], {"a.com", "b.com", "c.com"})

    def test_schema_snap_written_once_per_distinct_tenant(self) -> None:
        """A tenant has N domains but ONE schema, so put_schema_many is fed a deduped map."""
        t = make_tenant(schema_name="alpha")
        self._run(_rows(("a.com", t), ("b.com", t)))
        keys = sorted(k for k in self.fake_cache.store if k.startswith("schema-snap:"))
        self.assertEqual(keys, ["schema-snap:alpha"])

    def test_host_snaps_are_written_for_every_domain(self) -> None:
        t = make_tenant(schema_name="alpha")
        self._run(_rows(("a.com", t), ("b.com", t)))
        keys = sorted(k for k in self.fake_cache.store if k.startswith("host-snap:"))
        self.assertEqual(keys, ["host-snap:a.com", "host-snap:b.com"])


@override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
class ReconcileDirtyRecheckTests(SimpleTestCase):
    """reconcile re-runs the rebuild when a Domain mutation landed mid-build (the dirty
    counter moved), bounded at 3 attempts, then sweeps orphans ONCE over the final SET."""

    def _reconcile(self, dirty_values: Any) -> tuple[Any, ...]:
        """Run reconcile with _rebuild_once stubbed; `dirty_values` is what successive
        c.get(DIRTY_KEY) calls return (reconcile reads it before and after each rebuild).
        sweep_orphans is stubbed too — it is covered in test_resolve_cache.py, and isolating
        it keeps this test about the recheck loop."""
        c = FakeSetRedis()
        seq = iter(dirty_values)
        c.get = lambda name: next(seq)
        with mock.patch.object(resolve_cache, "redis_alive", return_value=True), \
             mock.patch.object(resolve_cache, "get_redis_raw_client", return_value=c), \
             mock.patch.object(resolve_cache, "sweep_orphans") as sweep, \
             mock.patch.object(HostRegistry, "_rebuild_once",
                               return_value=(1, {"a.com"})) as rebuild, \
             mock.patch("tenants.models.Tenant") as T:
            T.objects.values_list.return_value = ["alpha", "domainless"]
            n = host_registry.reconcile()
        return n, rebuild.call_count, sweep

    def test_stable_dirty_counter_runs_once(self) -> None:
        n, calls, sweep = self._reconcile(["7", "7"])
        self.assertEqual((n, calls), (1, 1))
        self.assertEqual(sweep.call_count, 1)                    # ONCE, after the final SET

    def test_mutation_midbuild_triggers_a_rerun(self) -> None:
        _, calls, _ = self._reconcile(["7", "8", "8", "8"])
        self.assertEqual(calls, 2)

    def test_reruns_are_bounded_at_three(self) -> None:
        """A domain mutated on every tick must not spin the reconcile forever."""
        _, calls, _ = self._reconcile(["1", "2", "3", "4", "5", "6"])
        self.assertEqual(calls, 3)

    def test_sweep_gets_all_tenant_schemas_not_only_those_with_domains(self) -> None:
        """valid_schemas must include DOMAINLESS tenants (which the Domain-driven rebuild
        never enumerates), else their lazily-filled schema-snap is swept every reconcile."""
        _, _, sweep = self._reconcile(["7", "7"])
        hosts, schemas = sweep.call_args[0]
        self.assertEqual(hosts, {"a.com"})
        self.assertEqual(schemas, {"alpha", "domainless"})

    def test_noop_when_warm_disabled(self) -> None:
        with override_settings(TENANT_REGISTRY={"WARM_ENABLED": False}):
            with mock.patch.object(HostRegistry, "_rebuild_once") as rebuild:
                self.assertEqual(host_registry.reconcile(), 0)
            rebuild.assert_not_called()

    def test_noop_when_redis_is_down(self) -> None:
        with mock.patch.object(resolve_cache, "redis_alive", return_value=False), \
             mock.patch.object(HostRegistry, "_rebuild_once") as rebuild:
            self.assertEqual(host_registry.reconcile(), 0)
        rebuild.assert_not_called()


class TtlForStatusTests(SimpleTestCase):
    """The TTL policy the WARM stage rests on: ACTIVE positives get NO expiry (the registry
    must survive memory pressure on a volatile-ttl instance, which never evicts a key
    without a TTL), transient statuses get short TTLs, and an unlisted status falls back to
    the flat positive TTL."""

    @override_settings(TENANT_RESOLVE={
        "POSITIVE_CACHE_SECONDS": 3600,
        "WARM_TTL_BY_STATUS": {
            "active": None, "deactivated": 3600, "failed": 1800, "new": 120, "pending": 120,
        },
    })
    def test_ttl_by_status_table(self) -> None:
        for status, expected in [
            (Tenant.Status.ACTIVE, None),          # no expiry — the load-bearing case
            (Tenant.Status.DEACTIVATED, 3600),
            (Tenant.Status.FAILED, 1800),
            (Tenant.Status.NEW, 120),
            (Tenant.Status.PENDING, 120),
        ]:
            with self.subTest(status=status):
                self.assertEqual(resolve_cache.ttl_for_status(status), expected)

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 3600,
                                       "WARM_TTL_BY_STATUS": {"active": None}})
    def test_unlisted_status_falls_back_to_the_flat_positive_ttl(self) -> None:
        self.assertEqual(resolve_cache.ttl_for_status("some_future_status"), 3600)

    def test_every_tenant_status_is_covered_by_the_default_mapping(self) -> None:
        """A status added to Tenant.Status without a WARM_TTL_BY_STATUS entry would silently
        inherit the flat TTL instead of a considered one — pin the enum against the mapping
        so the omission fails here instead of in production."""
        self.assertEqual(set(RESOLVE_DEFAULTS["WARM_TTL_BY_STATUS"]),
                         {s.value for s in Tenant.Status})
