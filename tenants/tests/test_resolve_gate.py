"""Tenant-resolve gate (WARM/GATE stages). DB-free: fake domain model + fake nx cache,
host_registry.check / fill_cap.allow patched or fed a fake Redis. See
deploy/resolve_gate_design.md."""
import logging
from typing import Any
from unittest import mock

from django.test import SimpleTestCase, override_settings
from redis.exceptions import RedisError

import tenants.middleware as mw
import tenants.resolver.service as service
from tenants.resolver import (
    ResolveDeferred, TenantResolveCache, resolve_cache, fill_cap,
)
from tenants.resolver import (
    DIRTY_KEY, WARM_LOCK_KEY, WARM_PENDING_KEY, HostRegistry, host_registry,
)

from ._support import (FakeLock, FakeLockRedis, FakeNxCache, make_domain_model,
                        make_tenant, use_resolve_cache)


class _FakeRedis:
    """Minimal pipeline supporting host_registry.check()."""

    def __init__(self, exists: bool, member: bool) -> None:
        self._exists, self._member = exists, member

    def pipeline(self) -> Any:
        outer = self

        class _Pipe:
            def exists(self, *a: Any) -> Any:
                return self

            def sismember(self, *a: Any) -> Any:
                return self

            def execute(self) -> list[int]:
                return [1 if outer._exists else 0, 1 if outer._member else 0]

        return _Pipe()


class HostRegistryCheckTests(SimpleTestCase):
    def _verdict(self, exists: bool, member: bool) -> object:
        with mock.patch.object(resolve_cache, "get_redis_raw_client",
                               return_value=_FakeRedis(exists, member)):
            return host_registry.check("h")

    def test_member(self) -> None:
        self.assertIs(self._verdict(exists=True, member=True), HostRegistry.MEMBER)

    def test_nonmember(self) -> None:
        self.assertIs(self._verdict(exists=True, member=False), HostRegistry.NONMEMBER)

    def test_set_absent_is_unknown(self) -> None:
        self.assertIs(self._verdict(exists=False, member=False), HostRegistry.UNKNOWN)

    def test_redis_error_is_unknown(self) -> None:
        boom = mock.Mock(side_effect=RedisError("redis down"))
        with mock.patch.object(resolve_cache, "get_redis_raw_client", boom):
            self.assertIs(host_registry.check("h"), HostRegistry.UNKNOWN)


@override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": True})
class GateMiddlewareTests(SimpleTestCase):
    def setUp(self) -> None:
        self.mw = mw.ShardAwareTenantMiddleware(lambda r: None)
        # trigger_warm must never touch Redis/celery in these unit tests
        self._tw = mock.patch.object(host_registry, "trigger_warm", lambda: None)
        self._tw.start()
        self.addCleanup(self._tw.stop)

    def _check(self, verdict: object) -> Any:
        return mock.patch.object(host_registry, "check", lambda h: verdict)

    def test_member_resolves_from_db(self) -> None:
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()), self._check(HostRegistry.MEMBER):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_nonmember_hard_reject_no_db_no_negative(self) -> None:
        dm = make_domain_model(make_tenant())      # tenant exists in DB, but SET says non-member
        fake = FakeNxCache()
        with use_resolve_cache(fake), self._check(HostRegistry.NONMEMBER):
            with self.assertRaises(dm.DoesNotExist):
                self.mw.get_tenant(dm, "known")
        self.assertEqual(dm.db_calls["n"], 0)       # rejected WITHOUT touching the DB
        self.assertNotIn("known", fake.store)       # and WITHOUT writing a negative

    def test_unknown_with_budget_falls_open_to_db(self) -> None:
        dm = make_domain_model(make_tenant())
        with use_resolve_cache(FakeNxCache()), self._check(HostRegistry.UNKNOWN), \
                mock.patch.object(fill_cap, "allow", lambda: True):
            got = self.mw.get_tenant(dm, "known")
        self.assertEqual(got.schema_name, "alpha")
        self.assertEqual(dm.db_calls["n"], 1)

    def test_unknown_without_budget_defers_no_db(self) -> None:
        """Flag-absent + budget spent => ResolveDeferred, NOT DoesNotExist.

        The gate never established that the host is unknown (that is what UNKNOWN means),
        it declined to look. Raising the caller's not_found here would surface a 404 —
        telling a legitimate tenant's users their workspace is gone — instead of the
        retryable 503 the middleware maps ResolveDeferred to."""
        dm = make_domain_model(make_tenant())
        fake = FakeNxCache()
        with use_resolve_cache(fake), self._check(HostRegistry.UNKNOWN), \
                mock.patch.object(fill_cap, "allow", lambda: False):
            with self.assertRaises(ResolveDeferred):
                self.mw.get_tenant(dm, "known")
        self.assertEqual(dm.db_calls["n"], 0)        # declined WITHOUT touching the DB
        self.assertNotIn("known", fake.store)        # and WITHOUT writing a negative

    def test_deferred_is_not_a_not_found(self) -> None:
        """A deferral must never be mistakable for 'no such tenant' by an except clause."""
        dm = make_domain_model(make_tenant())
        self.assertFalse(issubclass(ResolveDeferred, dm.DoesNotExist))


class StoreTtlByStatusTests(SimpleTestCase):
    class _RecCache:
        def __init__(self) -> None:
            self.calls = []

        def get(self, k: str, default: Any = None) -> Any:
            return default

        def set(self, key: str, value: Any, timeout: int | None = None, nx: bool = False,
                **kw: Any) -> bool:
            self.calls.append((key, timeout, nx))
            return True

    @override_settings(
        TENANT_REGISTRY={"WARM_ENABLED": True},
        TENANT_RESOLVE={"WARM_TTL_BY_STATUS": {"active": None, "deactivated": 3600}},
    )
    def test_active_no_ttl_deactivated_ttl(self) -> None:
        from tenants.models import Tenant
        rc = TenantResolveCache(cache=self._RecCache())
        rc.put("h1", make_tenant(status=Tenant.Status.ACTIVE))
        rc.put("h2", make_tenant(status=Tenant.Status.DEACTIVATED))
        by_key = {k: (ttl, nx) for k, ttl, nx in rc.cache.calls}
        self.assertEqual(by_key[rc._snap_key("h1")], (None, True))   # ACTIVE → no expiry, nx
        self.assertEqual(by_key[rc._snap_key("h2")], (3600, True))   # DEACTIVATED → 1h

    @override_settings(TENANT_REGISTRY={"WARM_ENABLED": False}, TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 3600})
    def test_legacy_flat_ttl_when_warm_off(self) -> None:
        rc = TenantResolveCache(cache=self._RecCache())
        rc.put("h", make_tenant())
        self.assertEqual(rc.cache.calls[0][1], 3600)    # flat _pos_ttl


class PutManyTests(SimpleTestCase):
    """Batched reconcile writer: one set_many per distinct TTL (no per-domain round-trips),
    force semantics (no nx, no hold-check)."""

    class _ManyCache:
        def __init__(self) -> None:
            self.calls = []                             # (set-of-keys, timeout)

        def set_many(self, mapping: dict[str, Any], timeout: int | None = None,
                    **kw: Any) -> None:
            self.calls.append((set(mapping), timeout))

    @override_settings(
        TENANT_REGISTRY={"WARM_ENABLED": True},
        TENANT_RESOLVE={"WARM_TTL_BY_STATUS": {"active": None, "deactivated": 3600}},
    )
    def test_groups_by_ttl_one_set_many_each(self) -> None:
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

    @override_settings(
        TENANT_REGISTRY={"WARM_ENABLED": True},
        TENANT_RESOLVE={"WARM_TTL_BY_STATUS": {"active": None, "deactivated": 3600}},
    )
    def test_schema_side_groups_by_ttl_too(self) -> None:
        """The schema-keyed sibling must group by TTL identically — both wrappers share
        _put_many_by_ttl, and without this the shared core is only guarded on the host side."""
        from tenants.models import Tenant
        rc = TenantResolveCache(cache=self._ManyCache())
        n = rc.put_schema_many([
            make_tenant(schema_name="s1", status=Tenant.Status.ACTIVE),
            make_tenant(schema_name="s2", status=Tenant.Status.DEACTIVATED),
            make_tenant(schema_name="s3", status=Tenant.Status.ACTIVE),
        ])
        self.assertEqual(n, 3)
        by_ttl = {ttl: keys for keys, ttl in rc.cache.calls}
        self.assertEqual(by_ttl[None], {rc._schema_snap_key("s1"), rc._schema_snap_key("s3")})
        self.assertEqual(by_ttl[3600], {rc._schema_snap_key("s2")})
        self.assertEqual(len(rc.cache.calls), 2)        # exactly one set_many per TTL


class CacheEnabledFlagTests(SimpleTestCase):
    """`enabled` gates the resolve short-circuit in service.resolve(). It must account for
    WARM: under WARM positives are written via ttl_by_status, so a zero flat TTL must NOT
    make the cache look disabled (finding #4)."""

    def _rc(self) -> TenantResolveCache:
        return TenantResolveCache(cache=FakeNxCache())

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 3600, "MISS_CACHE_SECONDS": 60},
                       TENANT_REGISTRY={"WARM_ENABLED": False})
    def test_enabled_with_flat_ttl(self) -> None:
        self.assertTrue(self._rc().enabled)

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 0, "MISS_CACHE_SECONDS": 0},
                       TENANT_REGISTRY={"WARM_ENABLED": False})
    def test_disabled_when_everything_off(self) -> None:
        self.assertFalse(self._rc().enabled)

    @override_settings(TENANT_RESOLVE={"POSITIVE_CACHE_SECONDS": 0, "MISS_CACHE_SECONDS": 0},
                       TENANT_REGISTRY={"WARM_ENABLED": True})
    def test_enabled_under_warm_even_with_zero_flat_ttl(self) -> None:
        self.assertTrue(self._rc().enabled)             # #4: WARM keeps the cache in use


@override_settings(TENANT_REGISTRY={"WARM_ENABLED": True})
class RunLockedFencingTests(SimpleTestCase):
    def _patches(self, fake: Any) -> tuple[Any, ...]:
        return (
            mock.patch.object(resolve_cache, "get_redis_raw_client", return_value=fake),
            mock.patch.object(resolve_cache, "redis_alive", return_value=True),
        )

    def test_acquires_reconciles_and_releases(self) -> None:
        lock = FakeLock(acquired=True)
        fake = FakeLockRedis(lock)
        p1, p2 = self._patches(fake)
        with p1, p2, mock.patch.object(host_registry, "reconcile", return_value=7):
            n = host_registry.run_locked()
        self.assertEqual(n, 7)
        self.assertEqual(fake.lock_calls[0][0], WARM_LOCK_KEY)   # locks the right key
        self.assertEqual(lock.acquire_calls, 1)
        self.assertEqual(lock.release_calls, 1)                  # fenced release fires

    def test_skips_when_lock_held(self) -> None:
        lock = FakeLock(acquired=False)            # someone else holds it
        fake = FakeLockRedis(lock)
        p1, p2 = self._patches(fake)
        with p1, p2, mock.patch.object(host_registry, "reconcile") as rec:
            n = host_registry.run_locked()
        self.assertIsNone(n)
        rec.assert_not_called()
        self.assertEqual(lock.release_calls, 0)     # never release a lock we didn't take

    def test_release_error_is_swallowed(self) -> None:
        lock = FakeLock(acquired=True, release_raises=True)     # expired mid-reconcile
        fake = FakeLockRedis(lock)
        p1, p2 = self._patches(fake)
        with p1, p2, mock.patch.object(host_registry, "reconcile", return_value=3):
            n = host_registry.run_locked()          # LockError must NOT propagate
        self.assertEqual(n, 3)


@override_settings(TENANT_REGISTRY={"WARM_ENABLED": True, "WARM_PENDING_SECONDS": 10})
class TriggerWarmCoalesceTests(SimpleTestCase):
    class _FakePendingRedis:
        def __init__(self) -> None:
            self.store = {}
            self.set_calls = []

        def set(self, key: str, val: Any, nx: bool = False, ex: int | None = None) -> bool | None:
            self.set_calls.append((key, val, nx, ex))
            if nx and key in self.store:
                return None
            self.store[key] = val
            return True

    def test_coalesces_enqueue_with_self_expiring_marker(self) -> None:
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
        def __init__(self, hosts_exists: bool) -> None:
            self._hosts_exists = hosts_exists
            self.members = set()
            self.sadd_calls = 0
            self.srem_calls = 0
            self.incr_keys = []
            self.expire_calls = []

        def exists(self, key: str) -> int:
            return 1 if self._hosts_exists else 0

        def sadd(self, key: str, member: str) -> None:
            self.sadd_calls += 1
            self.members.add(member)

        def srem(self, key: str, member: str) -> None:
            self.srem_calls += 1
            self.members.discard(member)

        # act as our own (no-op) pipeline for the dirty bump
        def pipeline(self) -> Any:
            return self

        def incr(self, key: str) -> Any:
            self.incr_keys.append(key)
            return self

        def expire(self, key: str, ttl: int, nx: bool = False) -> Any:
            self.expire_calls.append((key, ttl, nx))
            return self

        def execute(self) -> list[Any]:
            return []

    def _run(self, method: str, host: str, hosts_exists: bool) -> Any:
        fake = self._FakeRedis(hosts_exists)
        with mock.patch.object(resolve_cache, "get_redis_raw_client", return_value=fake):
            getattr(host_registry, method)(host)
        return fake

    def test_add_when_set_present_adds_member(self) -> None:
        fake = self._run("add", "h.example", hosts_exists=True)
        self.assertEqual(fake.sadd_calls, 1)
        self.assertIn("h.example", fake.members)

    def test_add_when_set_absent_does_not_resurrect(self) -> None:
        fake = self._run("add", "h.example", hosts_exists=False)
        self.assertEqual(fake.sadd_calls, 0)     # EXISTS guard → no SADD
        self.assertEqual(fake.members, set())    # SET stays absent → gate fail-open
        self.assertIn(DIRTY_KEY, fake.incr_keys) # but dirty IS bumped

    def test_remove_is_unconditional(self) -> None:
        fake = self._run("remove", "gone.example", hosts_exists=False)
        self.assertEqual(fake.srem_calls, 1)     # SREM issued even on an absent SET

    def test_dirty_bumped_with_bounded_nx_ttl(self) -> None:
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
    def test_gate_without_warm_is_treated_as_off(self) -> None:
        self.assertFalse(host_registry.gate_enabled)

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": True})
    def test_gate_with_warm_is_on(self) -> None:
        self.assertTrue(host_registry.gate_enabled)

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": False, "WARM_ENABLED": True})
    def test_gate_off_stays_off(self) -> None:
        self.assertFalse(host_registry.gate_enabled)

    # --- deploy-time system check tenants.E001 ---
    def _check(self) -> list[Any]:
        from tenants.checks import gate_requires_warm
        return gate_requires_warm(app_configs=None)

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": False})
    def test_check_errors_on_gate_without_warm(self) -> None:
        errs = self._check()
        self.assertEqual([e.id for e in errs], ["tenants.E001"])

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": True, "WARM_ENABLED": True})
    def test_check_ok_when_both_on(self) -> None:
        self.assertEqual(self._check(), [])

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": False, "WARM_ENABLED": True})
    def test_check_ok_warm_only(self) -> None:        # valid rollout intermediate (Stage 1)
        self.assertEqual(self._check(), [])

    @override_settings(TENANT_REGISTRY={"GATE_ENABLED": False, "WARM_ENABLED": False})
    def test_check_ok_both_off(self) -> None:         # today's default
        self.assertEqual(self._check(), [])


class SingleFlightTests(SimpleTestCase):
    """coalescing: leader shares a real Exception with followers, but NOT control-flow
    exceptions (which belong to the leader thread); a follower left without a value
    self-resolves instead of returning a bogus None. White-box via throttle._inflight."""

    def setUp(self) -> None:
        from tenants.resolver import throttle
        self.t = throttle
        self.addCleanup(self.t._inflight.clear)

    def _seat_follower(self, key: str, *, result: Any, exc: BaseException | None) -> None:
        # Pre-seat a completed slot so the next call takes the FOLLOWER branch.
        ev = __import__("threading").Event(); ev.set()
        self.t._inflight[key] = {"event": ev, "result": result, "exc": exc}

    def test_leader_shares_real_exception(self) -> None:
        with self.assertRaises(ValueError):
            self.t.single_flight("k", lambda: (_ for _ in ()).throw(ValueError("boom")))
        self.assertNotIn("k", self.t._inflight)         # slot cleaned in finally

    def test_leader_control_flow_propagates_and_cleans_slot(self) -> None:
        def boom() -> None:
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):      # NOT swallowed
            self.t.single_flight("k", boom)
        self.assertNotIn("k", self.t._inflight)         # finally still cleaned up

    def test_follower_inherits_real_exception(self) -> None:
        self._seat_follower("k", result=self.t._UNSET, exc=ValueError("shared"))
        with self.assertRaises(ValueError):
            self.t.single_flight("k", lambda: "own")

    def test_follower_self_resolves_when_leader_left_no_value(self) -> None:
        # Leader aborted via control-flow → result stays _UNSET, exc None.
        self._seat_follower("k", result=self.t._UNSET, exc=None)
        got = self.t.single_flight("k", lambda: "own")
        self.assertEqual(got, "own")                    # self-resolved, NOT None

    def test_follower_shares_leader_result(self) -> None:
        self._seat_follower("k", result="leader-value", exc=None)
        got = self.t.single_flight("k", lambda: "own")
        self.assertEqual(got, "leader-value")           # took the shared result, no own resolve


class ThrottledLogTests(SimpleTestCase):
    """service._ThrottledLog — at most one line per _LOG_EVERY window, the rest counted.

    Built on a FRESH instance rather than by poking module globals (which is what this had to
    do while the three call sites each owned a pair of them), so nothing here leaks between
    tests and the behaviour is asserted once for all three sites.
    """

    def _throttle(self, **kw: Any) -> Any:
        return service._ThrottledLog(logging.WARNING, "x %r%s", **kw)

    def test_first_emits_then_suppresses_within_the_window(self) -> None:
        t = self._throttle()
        with mock.patch.object(service.time, "monotonic", return_value=1000.0), \
             mock.patch.object(service.logger, "log") as log:
            for _ in range(5):
                t("h")
        self.assertEqual(log.call_count, 1)             # only the first within the window
        self.assertEqual(t._suppressed, 4)              # the other 4 counted

    def test_emits_again_after_the_window_with_the_suppressed_count(self) -> None:
        t = self._throttle()
        t._last, t._suppressed = 1000.0, 7
        with mock.patch.object(service.time, "monotonic",
                               return_value=1000.0 + service._LOG_EVERY), \
             mock.patch.object(service.logger, "log") as log:
            t("h")
        log.assert_called_once()
        args = log.call_args[0]
        self.assertIn("7 similar suppressed", args[1] % args[2:])
        self.assertEqual(t._suppressed, 0)              # reset after emit

    def test_the_window_boundary_is_inclusive(self) -> None:
        """>= _LOG_EVERY emits: a strict > would stall a wave landing exactly on the tick."""
        t = self._throttle()
        t._last = 1000.0
        for delta, expected in ((service._LOG_EVERY - 0.001, 0), (service._LOG_EVERY, 1)):
            with self.subTest(delta=delta):
                t._last = 1000.0
                with mock.patch.object(service.time, "monotonic",
                                       return_value=1000.0 + delta), \
                     mock.patch.object(service.logger, "log") as log:
                    t("h")
                self.assertEqual(log.call_count, expected)

    def test_a_suppressed_call_leaves_the_window_start_alone(self) -> None:
        """Counting must not extend the window — otherwise sustained traffic logs NOTHING."""
        t = self._throttle()
        with mock.patch.object(service.time, "monotonic", return_value=1000.0), \
             mock.patch.object(service.logger, "log"):
            t("h")                                      # emits, sets _last = 1000.0
        with mock.patch.object(service.time, "monotonic", return_value=1020.0), \
             mock.patch.object(service.logger, "log"):
            t("h")                                      # suppressed
        self.assertEqual(t._last, 1000.0)

    def test_exc_info_is_forwarded(self) -> None:
        for exc_info in (True, False):
            with self.subTest(exc_info=exc_info):
                t = self._throttle(exc_info=exc_info)
                with mock.patch.object(service.time, "monotonic", return_value=1000.0), \
                     mock.patch.object(service.logger, "log") as log:
                    t("h")
                self.assertIs(log.call_args[1]["exc_info"], exc_info)


class ResolveLoggerWiringTests(SimpleTestCase):
    """Each call site gets the level and exc_info its comment claims.

    Previously unpinned for two of the three: the whole point of splitting them is that an
    unexpected cache-path error is LOUD (so a bug cannot masquerade as "slower") while expected
    infra failure and a deliberate load-shed are not, and a shed carries no exception at all.
    """

    def test_levels_and_exc_info(self) -> None:
        for name, level, exc_info in (
            ("_log_cache_fail", logging.WARNING, True),
            ("_log_cache_bug", logging.ERROR, True),
            ("_log_shed", logging.WARNING, False),
        ):
            with self.subTest(name=name):
                fn = getattr(service, name)
                self.assertEqual(fn._level, level)
                self.assertIs(fn._exc_info, exc_info)

    def test_every_template_takes_the_host_then_the_suppressed_suffix(self) -> None:
        """_ThrottledLog always passes (hostname, extra) in that order."""
        for name in ("_log_cache_fail", "_log_cache_bug", "_log_shed"):
            with self.subTest(name=name):
                rendered = getattr(service, name)._template % ("h.example.com", " (2 similar)")
                self.assertIn("'h.example.com'", rendered)
                self.assertTrue(rendered.endswith(" (2 similar)"))


class ConfigNamespaceTests(SimpleTestCase):
    """config._Namespace: per-key merge over DEFAULTS + robust __getattr__ (no recursion on
    a pre-init/copied instance, clear error on an unknown key)."""

    def test_merge_over_defaults(self) -> None:
        from tenants.resolver.config import resolve_cfg
        with override_settings(TENANT_RESOLVE={"HOLD_SECONDS": 8}):
            self.assertEqual(resolve_cfg.HOLD_SECONDS, 8)                 # user value
            self.assertEqual(resolve_cfg.POSITIVE_CACHE_SECONDS, 3600)    # falls to DEFAULTS

    def test_unknown_key_raises_attributeerror(self) -> None:
        from tenants.resolver.config import resolve_cfg
        with self.assertRaises(AttributeError):
            resolve_cfg.NOPE_KEY

    def test_no_recursion_before_init(self) -> None:
        from tenants.resolver.config import _Namespace
        ns = _Namespace.__new__(_Namespace)          # bypass __init__ → _defaults NOT set
        self.assertFalse(hasattr(ns, "anything"))    # must not RecursionError
        self.assertFalse(hasattr(ns, "__deepcopy__"))

    def test_deepcopy_ok(self) -> None:
        import copy
        from tenants.resolver.config import resolve_cfg
        dup = copy.deepcopy(resolve_cfg)             # exercises __deepcopy__/reduce probes
        self.assertEqual(dup.HOLD_SECONDS, resolve_cfg.HOLD_SECONDS)


class ResolveFailOpenRoutingTests(SimpleTestCase):
    """service.resolve() exception policy: not_found / OperationalError propagate; RedisError
    (infra) fails open QUIETLY; any other error (a bug) still fails open but LOUD. Fail-open is
    safe because db_resolver() returns the correct tenant. (enabled is True by default:
    POSITIVE_CACHE_SECONDS=3600, so resolve() enters the cache path.)"""
    class NotFound(Exception):
        pass

    def _run(self, exc: BaseException) -> tuple[Any, ...]:
        from tenants.resolver import service
        sent = object()
        with mock.patch.object(service, "_via_cache", side_effect=exc), \
             mock.patch.object(service, "_log_cache_fail") as fail, \
             mock.patch.object(service, "_log_cache_bug") as bug:
            result = service.resolve("h", lambda: sent, self.NotFound)
        return result, sent, fail, bug

    def test_not_found_propagates(self) -> None:
        from tenants.resolver import service
        with mock.patch.object(service, "_via_cache", side_effect=self.NotFound):
            with self.assertRaises(self.NotFound):
                service.resolve("h", lambda: None, self.NotFound)

    def test_operational_error_propagates(self) -> None:
        from django.db import OperationalError
        from tenants.resolver import service
        with mock.patch.object(service, "_via_cache", side_effect=OperationalError):
            with self.assertRaises(OperationalError):
                service.resolve("h", lambda: None, self.NotFound)

    def test_redis_error_fails_open_quietly(self) -> None:
        from redis.exceptions import RedisError
        result, sent, fail, bug = self._run(RedisError("down"))
        self.assertIs(result, sent)          # fail-open to DB
        fail.assert_called_once()            # infra → WARNING path
        bug.assert_not_called()

    def test_unexpected_bug_fails_open_loudly(self) -> None:
        result, sent, fail, bug = self._run(TypeError("boom"))
        self.assertIs(result, sent)          # still fail-open (DB is correct)
        bug.assert_called_once()             # bug → ERROR path
        fail.assert_not_called()


class ResolveLoggerIndependenceTests(SimpleTestCase):
    """Each call site keeps its OWN window and counter, so a bug is never masked by
    infra-warning noise — the reason the three are separate rather than one shared throttle.

    Drives the REAL module-level instances (restored on teardown), because the thing under
    test is how they were wired, not _ThrottledLog itself.
    """

    SITES = ("_log_cache_fail", "_log_cache_bug", "_log_shed")

    def setUp(self) -> None:
        for name in self.SITES:
            t = getattr(service, name)
            self.addCleanup(setattr, t, "_last", t._last)
            self.addCleanup(setattr, t, "_suppressed", t._suppressed)
            t._last, t._suppressed = 0.0, 0

    def test_each_site_throttles_on_its_own_counter(self) -> None:
        with mock.patch.object(service.time, "monotonic", return_value=2000.0), \
             mock.patch.object(service.logger, "log") as log:
            for _ in range(3):
                service._log_cache_bug("h")
            service._log_cache_fail("h")      # a DIFFERENT site: still gets its first hit
        self.assertEqual(log.call_count, 2)   # one per site, not one in total
        self.assertEqual(service._log_cache_bug._suppressed, 2)
        self.assertEqual(service._log_cache_fail._suppressed, 0)
        self.assertEqual(service._log_shed._suppressed, 0)
        self.assertEqual(log.call_args_list[0][0][0], logging.ERROR)     # the bug, LOUD
        self.assertEqual(log.call_args_list[1][0][0], logging.WARNING)   # the infra failure
