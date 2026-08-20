"""Tenant-resolve gate (WARM/GATE stages). DB-free: fake domain model + fake nx cache,
host_registry.check / fill_cap.allow patched or fed a fake Redis. See
deploy/resolve_gate_design.md."""
from unittest import mock

from django.test import SimpleTestCase, override_settings
from redis.exceptions import RedisError

import tenants.middleware as mw
from tenants.resolver import TenantResolveCache, resolve_cache, fill_cap
from tenants.resolver import (
    DIRTY_KEY, WARM_LOCK_KEY, WARM_PENDING_KEY, HostRegistry, host_registry,
)

from ._support import FakeNxCache, make_domain_model, make_tenant, use_resolve_cache


class _FakeRedis:
    """Minimal pipeline supporting host_registry.check()."""

    def __init__(self, exists, member):
        self._exists, self._member = exists, member

    def pipeline(self):
        outer = self

        class _Pipe:
            def exists(self, *a):
                return self

            def sismember(self, *a):
                return self

            def execute(self):
                return [1 if outer._exists else 0, 1 if outer._member else 0]

        return _Pipe()


class HostRegistryCheckTests(SimpleTestCase):
    def _verdict(self, exists, member):
        with mock.patch.object(resolve_cache, "get_redis_raw_client",
                               return_value=_FakeRedis(exists, member)):
            return host_registry.check("h")

    def test_member(self):
        self.assertIs(self._verdict(exists=True, member=True), HostRegistry.MEMBER)

    def test_nonmember(self):
        self.assertIs(self._verdict(exists=True, member=False), HostRegistry.NONMEMBER)

    def test_set_absent_is_unknown(self):
        self.assertIs(self._verdict(exists=False, member=False), HostRegistry.UNKNOWN)

    def test_redis_error_is_unknown(self):
        boom = mock.Mock(side_effect=RedisError("redis down"))
        with mock.patch.object(resolve_cache, "get_redis_raw_client", boom):
            self.assertIs(host_registry.check("h"), HostRegistry.UNKNOWN)


@override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": True})
class GateMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)
        # trigger_warm must never touch Redis/celery in these unit tests
        self._tw = mock.patch.object(host_registry, "trigger_warm", lambda: None)
        self._tw.start()
        self.addCleanup(self._tw.stop)

    def _check(self, verdict):
        return mock.patch.object(host_registry, "check", lambda h: verdict)

    def test_member_resolves_from_db(self):
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()), self._check(HostRegistry.MEMBER):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_nonmember_hard_reject_no_db_no_negative(self):
        dm = make_domain_model(make_tenant())      # tenant exists in DB, but SET says non-member
        fake = FakeNxCache()
        with use_resolve_cache(fake), self._check(HostRegistry.NONMEMBER):
            with self.assertRaises(dm.DoesNotExist):
                self.mw.get_tenant(dm, "known")
        self.assertEqual(dm.db_calls["n"], 0)       # rejected WITHOUT touching the DB
        self.assertNotIn("known", fake.store)       # and WITHOUT writing a negative

    def test_unknown_with_budget_falls_open_to_db(self):
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()), self._check(HostRegistry.UNKNOWN), \
                mock.patch.object(fill_cap, "allow", lambda: True):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_unknown_without_budget_rejects_no_db(self):
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()), self._check(HostRegistry.UNKNOWN), \
                mock.patch.object(fill_cap, "allow", lambda: False):
            with self.assertRaises(dm.DoesNotExist):
                self.mw.get_tenant(dm, "known")
        self.assertEqual(dm.db_calls["n"], 0)


class StoreTtlByStatusTests(SimpleTestCase):
    class _RecCache:
        def __init__(self):
            self.calls = []

        def get(self, k, default=None):
            return default

        def set(self, key, value, timeout=None, nx=False, **kw):
            self.calls.append((key, timeout, nx))
            return True

    @override_settings(
        TENANT_REGISTRY={"WARM_ENABLED": True},
        TENANT_RESOLVE={"WARM_TTL_BY_STATUS": {"active": None, "deactivated": 3600}},
    )
    def test_active_no_ttl_deactivated_ttl(self):
        from tenants.models import Tenant
        rc = TenantResolveCache(cache=self._RecCache())
        rc.store("h1", make_tenant(status=Tenant.Status.ACTIVE))
        rc.store("h2", make_tenant(status=Tenant.Status.DEACTIVATED))
        by_key = {k: (ttl, nx) for k, ttl, nx in rc.cache.calls}
        self.assertEqual(by_key[rc._snap_key("h1")], (None, True))   # ACTIVE → no expiry, nx
        self.assertEqual(by_key[rc._snap_key("h2")], (3600, True))   # DEACTIVATED → 1h

    @override_settings(TENANT_REGISTRY={"WARM_ENABLED": False}, TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 3600})
    def test_legacy_flat_ttl_when_warm_off(self):
        rc = TenantResolveCache(cache=self._RecCache())
        rc.store("h", make_tenant())
        self.assertEqual(rc.cache.calls[0][1], 3600)    # flat _pos_ttl


class PutManyTests(SimpleTestCase):
    """Batched reconcile writer: one set_many per distinct TTL (no per-domain round-trips),
    force semantics (no nx, no hold-check)."""

    class _ManyCache:
        def __init__(self):
            self.calls = []                             # (set-of-keys, timeout)

        def set_many(self, mapping, timeout=None, **kw):
            self.calls.append((set(mapping), timeout))

    @override_settings(
        TENANT_REGISTRY={"WARM_ENABLED": True},
        TENANT_RESOLVE={"WARM_TTL_BY_STATUS": {"active": None, "deactivated": 3600}},
    )
    def test_groups_by_ttl_one_set_many_each(self):
        from tenants.models import Tenant
        rc = TenantResolveCache(cache=self._ManyCache())
        n = rc.put_many([
            ("h1", make_tenant(status=Tenant.Status.ACTIVE)),
            ("h2", make_tenant(status=Tenant.Status.DEACTIVATED)),
            ("h3", make_tenant(status=Tenant.Status.ACTIVE)),
        ])
        self.assertEqual(n, 3)
        by_ttl = {ttl: keys for keys, ttl in rc.cache.calls}
        self.assertEqual(by_ttl[None], {rc._snap_key("h1"), rc._snap_key("h3")})  # ACTIVE → no expiry
        self.assertEqual(by_ttl[3600], {rc._snap_key("h2")})                      # DEACTIVATED → 1h
        self.assertEqual(len(rc.cache.calls), 2)        # exactly one set_many per TTL


class CacheEnabledFlagTests(SimpleTestCase):
    """`enabled` gates the resolve short-circuit in service.resolve(). It must account for
    WARM: under WARM positives are written via ttl_by_status, so a zero flat TTL must NOT
    make the cache look disabled (finding #4)."""

    def _rc(self):
        return TenantResolveCache(cache=FakeNxCache())

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 3600, "MISS_CACHE_SECONDS": 60},
                       TENANT_REGISTRY={"WARM_ENABLED": False})
    def test_enabled_with_flat_ttl(self):
        self.assertTrue(self._rc().enabled)

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 0, "MISS_CACHE_SECONDS": 0},
                       TENANT_REGISTRY={"WARM_ENABLED": False})
    def test_disabled_when_everything_off(self):
        self.assertFalse(self._rc().enabled)

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 0, "MISS_CACHE_SECONDS": 0},
                       TENANT_REGISTRY={"WARM_ENABLED": True})
    def test_enabled_under_warm_even_with_zero_flat_ttl(self):
        self.assertTrue(self._rc().enabled)             # #4: WARM keeps the cache in use


class _FakeLock:
    """Stand-in for redis-py's Lock: records acquire/release; release() can raise LockError
    to simulate a lock that expired mid-reconcile (no longer ours)."""

    def __init__(self, acquired=True, release_raises=False):
        self._acquired = acquired
        self._release_raises = release_raises
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, blocking=True, **kw):
        self.acquire_calls += 1
        return self._acquired

    def release(self):
        self.release_calls += 1
        if self._release_raises:
            from redis.exceptions import LockError
            raise LockError("not owned")


class _FakeLockRedis:
    """Returns a preset _FakeLock from .lock(); records the lock() call args."""

    def __init__(self, lock):
        self._lock = lock
        self.lock_calls = []

    def lock(self, name, timeout=None, **kw):
        self.lock_calls.append((name, timeout))
        return self._lock


@override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
class RunLockedFencingTests(SimpleTestCase):
    def _patches(self, fake):
        return (
            mock.patch.object(resolve_cache, "get_redis_raw_client", return_value=fake),
            mock.patch.object(resolve_cache, "redis_alive", return_value=True),
        )

    def test_acquires_reconciles_and_releases(self):
        lock = _FakeLock(acquired=True)
        fake = _FakeLockRedis(lock)
        p1, p2 = self._patches(fake)
        with p1, p2, mock.patch.object(host_registry, "reconcile", return_value=7):
            n = host_registry.run_locked()
        self.assertEqual(n, 7)
        self.assertEqual(fake.lock_calls[0][0], WARM_LOCK_KEY)   # locks the right key
        self.assertEqual(lock.acquire_calls, 1)
        self.assertEqual(lock.release_calls, 1)                  # fenced release fires

    def test_skips_when_lock_held(self):
        lock = _FakeLock(acquired=False)            # someone else holds it
        fake = _FakeLockRedis(lock)
        p1, p2 = self._patches(fake)
        with p1, p2, mock.patch.object(host_registry, "reconcile") as rec:
            n = host_registry.run_locked()
        self.assertIsNone(n)
        rec.assert_not_called()
        self.assertEqual(lock.release_calls, 0)     # never release a lock we didn't take

    def test_release_error_is_swallowed(self):
        lock = _FakeLock(acquired=True, release_raises=True)     # expired mid-reconcile
        fake = _FakeLockRedis(lock)
        p1, p2 = self._patches(fake)
        with p1, p2, mock.patch.object(host_registry, "reconcile", return_value=3):
            n = host_registry.run_locked()          # LockError must NOT propagate
        self.assertEqual(n, 3)


@override_settings(TENANT_REGISTRY={"WARM_ENABLED": True, "WARM_PENDING_SECONDS": 10})
class TriggerWarmCoalesceTests(SimpleTestCase):
    class _FakePendingRedis:
        def __init__(self):
            self.store = {}
            self.set_calls = []

        def set(self, key, val, nx=False, ex=None):
            self.set_calls.append((key, val, nx, ex))
            if nx and key in self.store:
                return None
            self.store[key] = val
            return True

    def test_coalesces_enqueue_with_self_expiring_marker(self):
        import tenants.tasks as tasks_mod
        fake = self._FakePendingRedis()
        with mock.patch.object(resolve_cache, "get_redis_raw_client", return_value=fake), \
                mock.patch.object(tasks_mod.reconcile_host_registry_task, "delay") as delay:
            host_registry.trigger_warm()            # 1st: sets marker + enqueues
            host_registry.trigger_warm()            # 2nd: marker present → coalesced (no enqueue)
        self.assertEqual(delay.call_count, 1)
        pend = next(c for c in fake.set_calls if c[0] == WARM_PENDING_KEY)
        self.assertTrue(pend[2])                    # nx=True
        self.assertEqual(pend[3], 10)               # ex == configured TTL (self-expiring)


@override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
class ApplyMembershipTests(SimpleTestCase):
    """add()/remove() maintain treg:hosts WITHOUT Lua: SADD is EXISTS-guarded in app code
    (never resurrects an absent SET — #1), SREM is unconditional, and the dirty counter is
    always bumped (pipelined) with a bounded NX ttl."""

    class _FakeRedis:
        def __init__(self, hosts_exists):
            self._hosts_exists = hosts_exists
            self.members = set()
            self.sadd_calls = 0
            self.srem_calls = 0
            self.incr_keys = []
            self.expire_calls = []

        def exists(self, key):
            return 1 if self._hosts_exists else 0

        def sadd(self, key, member):
            self.sadd_calls += 1
            self.members.add(member)

        def srem(self, key, member):
            self.srem_calls += 1
            self.members.discard(member)

        # act as our own (no-op) pipeline for the dirty bump
        def pipeline(self):
            return self

        def incr(self, key):
            self.incr_keys.append(key)
            return self

        def expire(self, key, ttl, nx=False):
            self.expire_calls.append((key, ttl, nx))
            return self

        def execute(self):
            return []

    def _run(self, method, host, hosts_exists):
        fake = self._FakeRedis(hosts_exists)
        with mock.patch.object(resolve_cache, "get_redis_raw_client", return_value=fake):
            getattr(host_registry, method)(host)
        return fake

    def test_add_when_set_present_adds_member(self):
        fake = self._run("add", "h.example", hosts_exists=True)
        self.assertEqual(fake.sadd_calls, 1)
        self.assertIn("h.example", fake.members)

    def test_add_when_set_absent_does_not_resurrect(self):
        fake = self._run("add", "h.example", hosts_exists=False)
        self.assertEqual(fake.sadd_calls, 0)     # EXISTS guard → no SADD
        self.assertEqual(fake.members, set())    # SET stays absent → gate fail-open
        self.assertIn(DIRTY_KEY, fake.incr_keys) # but dirty IS bumped

    def test_remove_is_unconditional(self):
        fake = self._run("remove", "gone.example", hosts_exists=False)
        self.assertEqual(fake.srem_calls, 1)     # SREM issued even on an absent SET

    def test_dirty_bumped_with_bounded_nx_ttl(self):
        fake = self._run("add", "h.example", hosts_exists=True)
        self.assertIn(DIRTY_KEY, fake.incr_keys)
        self.assertTrue(
            any(k == DIRTY_KEY and ttl and nx for (k, ttl, nx) in fake.expire_calls),
            f"expected expire(DIRTY_KEY, ttl, nx=True); got {fake.expire_calls}",
        )


class GateRequiresWarmTests(SimpleTestCase):
    """GATE ⇒ WARM invariant: fail-safe in code (gate_enabled) + loud at deploy (E001)."""

    # --- runtime fail-safe: gate_enabled is effective ONLY with WARM on ---
    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": False})
    def test_gate_without_warm_is_treated_as_off(self):
        self.assertFalse(host_registry.gate_enabled)

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": True})
    def test_gate_with_warm_is_on(self):
        self.assertTrue(host_registry.gate_enabled)

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": False, "WARM_ENABLED": True})
    def test_gate_off_stays_off(self):
        self.assertFalse(host_registry.gate_enabled)

    # --- deploy-time system check tenants.E001 ---
    def _check(self):
        from tenants.checks import gate_requires_warm
        return gate_requires_warm(app_configs=None)

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": False})
    def test_check_errors_on_gate_without_warm(self):
        errs = self._check()
        self.assertEqual([e.id for e in errs], ["tenants.E001"])

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": True})
    def test_check_ok_when_both_on(self):
        self.assertEqual(self._check(), [])

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": False, "WARM_ENABLED": True})
    def test_check_ok_warm_only(self):        # valid rollout intermediate (Stage 1)
        self.assertEqual(self._check(), [])

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": False, "WARM_ENABLED": False})
    def test_check_ok_both_off(self):         # today's default
        self.assertEqual(self._check(), [])


class SingleFlightTests(SimpleTestCase):
    """coalescing: leader shares a real Exception with followers, but NOT control-flow
    exceptions (which belong to the leader thread); a follower left without a value
    self-resolves instead of returning a bogus None. White-box via throttle._inflight."""

    def setUp(self):
        from tenants.resolver import throttle
        self.t = throttle
        self.addCleanup(self.t._inflight.clear)

    def _seat_follower(self, key, *, result, exc):
        # Pre-seat a completed slot so the next call takes the FOLLOWER branch.
        ev = __import__("threading").Event(); ev.set()
        self.t._inflight[key] = {"event": ev, "result": result, "exc": exc}

    def test_leader_shares_real_exception(self):
        with self.assertRaises(ValueError):
            self.t.single_flight("k", lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertNotIn("k", self.t._inflight)         # slot cleaned in finally

    def test_leader_control_flow_propagates_and_cleans_slot(self):
        def boom():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):      # NOT swallowed
            self.t.single_flight("k", boom)
        self.assertNotIn("k", self.t._inflight)         # finally still cleaned up

    def test_follower_inherits_real_exception(self):
        self._seat_follower("k", result=self.t._UNSET, exc=ValueError("shared"))
        with self.assertRaises(ValueError):
            self.t.single_flight("k", lambda: "own")

    def test_follower_self_resolves_when_leader_left_no_value(self):
        # Leader aborted via control-flow → result stays _UNSET, exc None.
        self._seat_follower("k", result=self.t._UNSET, exc=None)
        got = self.t.single_flight("k", lambda: "own")
        self.assertEqual(got, "own")                    # self-resolved, NOT None

    def test_follower_shares_leader_result(self):
        self._seat_follower("k", result="leader-value", exc=None)
        got = self.t.single_flight("k", lambda: "own")
        self.assertEqual(got, "leader-value")           # took the shared result, no own resolve


class FailOpenLogThrottleTests(SimpleTestCase):
    """The fail-open branch logs at most one traceback per _FAIL_LOG_EVERY window and
    counts the rest — no one-traceback-per-request storm under a sustained cache failure."""

    def setUp(self):
        from tenants.resolver import service
        self.svc = service
        self.svc._fail_last = 0.0
        self.svc._fail_suppressed = 0
        self.addCleanup(setattr, self.svc, "_fail_last", 0.0)
        self.addCleanup(setattr, self.svc, "_fail_suppressed", 0)

    def test_first_logs_then_suppresses_within_window(self):
        with mock.patch.object(self.svc.time, "monotonic", return_value=1000.0), \
                mock.patch.object(self.svc.logger, "warning") as warn:
            for _ in range(5):
                self.svc._log_cache_fail("h")
        self.assertEqual(warn.call_count, 1)            # only the first within the window
        self.assertEqual(self.svc._fail_suppressed, 4)  # the other 4 counted

    def test_emits_again_after_window_with_suppressed_count(self):
        self.svc._fail_last = 1000.0
        self.svc._fail_suppressed = 7
        after = 1000.0 + self.svc._FAIL_LOG_EVERY
        with mock.patch.object(self.svc.time, "monotonic", return_value=after), \
                mock.patch.object(self.svc.logger, "warning") as warn:
            self.svc._log_cache_fail("h")
        warn.assert_called_once()
        msg = warn.call_args[0][0] % warn.call_args[0][1:]
        self.assertIn("7 similar suppressed", msg)
        self.assertEqual(self.svc._fail_suppressed, 0)  # reset after emit


class ConfigNamespaceTests(SimpleTestCase):
    """config._Namespace: per-key merge over DEFAULTS + robust __getattr__ (no recursion on
    a pre-init/copied instance, clear error on an unknown key)."""

    def test_merge_over_defaults(self):
        from tenants.resolver.config import resolve_cfg
        with override_settings(TENANT_RESOLVE={"HOLD_SECONDS": 8}):
            self.assertEqual(resolve_cfg.HOLD_SECONDS, 8)                 # user value
            self.assertEqual(resolve_cfg.POSITIVE_CACHE_SECONDS, 3600)    # falls to DEFAULTS

    def test_unknown_key_raises_attributeerror(self):
        from tenants.resolver.config import resolve_cfg
        with self.assertRaises(AttributeError):
            resolve_cfg.NOPE_KEY

    def test_no_recursion_before_init(self):
        from tenants.resolver.config import _Namespace
        ns = _Namespace.__new__(_Namespace)          # bypass __init__ → _defaults NOT set
        self.assertFalse(hasattr(ns, "anything"))    # must not RecursionError
        self.assertFalse(hasattr(ns, "__deepcopy__"))

    def test_deepcopy_ok(self):
        import copy
        from tenants.resolver.config import resolve_cfg
        dup = copy.deepcopy(resolve_cfg)             # exercises __deepcopy__/reduce probes
        self.assertEqual(dup.HOLD_SECONDS, resolve_cfg.HOLD_SECONDS)
