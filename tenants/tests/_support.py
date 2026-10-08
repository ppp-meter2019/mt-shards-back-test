"""Shared DB-free test helpers for the tenant-resolution cache & gate.

These tests are SimpleTestCase (no DB): the DB and the Redis cache are replaced by
fakes/mocks, so they run without Postgres or Redis. Tests that genuinely need the DB
(invalidation wiring, immutable-alias clean(), the login schema-stamp) are a
separate, DB-backed follow-up harness.
"""
from collections.abc import Iterable, Iterator
from typing import Any
from contextlib import contextmanager
from unittest import mock

from tenants.models import Shard, Tenant


class FakeNxCache:
    """Minimal in-memory cache covering what get_tenant / forget_hosts use,
    including the django_redis-only nx=True flag."""

    # django_redis attributes TenantResolveCache._snapshot_key_prefix() reads, so the
    # SCAN-based iter_snapshot_hosts / iter_snapshot_schemas work against this double too.
    key_prefix = "tres"
    version = 1

    def __init__(self) -> None:
        self.store = {}
        self.pattern_calls = []        # every delete_pattern() argument, in order

    def get(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    def set(self, key: str, value: Any, timeout: int | None = None, nx: bool = False,
            **kw: Any) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def set_many(self, mapping: dict[str, Any], timeout: int | None = None, **kw: Any) -> None:
        self.store.update(mapping)

    def get_many(self, keys: Iterable[str]) -> dict[str, Any]:
        return {k: self.store[k] for k in keys if k in self.store}

    def delete_many(self, keys: Iterable[str]) -> None:
        for k in keys:
            self.store.pop(k, None)

    def delete_pattern(self, pattern: str) -> int:
        """The `<prefix>*` subset of glob that this project actually passes.

        The real django_redis delete_pattern SCANs `KEY_PREFIX:version:<pattern>` and deletes
        only the matches, so the pattern is the ONLY thing that decides the scope. A double
        that clears the whole store makes every prefix-scoped sweep look alike — sweeping one
        namespace instead of two, or a mistyped prefix, both come out identical. forget_all()
        passes two distinct non-"*" patterns, so that difference is the thing under test.

        Anything beyond `<prefix>*` fails loudly rather than being silently approximated.
        """
        if not pattern.endswith("*") or "*" in pattern[:-1]:
            raise AssertionError(f"unsimulated pattern {pattern!r} — extend this double")
        self.pattern_calls.append(pattern)
        victims = [k for k in self.store if k.startswith(pattern[:-1])]
        for k in victims:
            del self.store[k]
        return len(victims)


class FakeSetRedis:
    """Raw-client double for the `treg:*` SET operations used by HostRegistry.reconcile /
    _rebuild_once. Keys are modelled as {name: set|str}.

    It records the op ORDER in `.ops`, because the invariant under test is sequencing as
    much as the end state: the SET must be built in `treg:hosts:new` and only then RENAMEd
    over `treg:hosts` — a direct SADD into the live key would publish a half-built SET and
    make the gate reject live hosts.
    """

    def __init__(self, dirty: Any = None) -> None:
        self.keys = {}
        self.ops = []
        self.dirty = dirty                     # what get(DIRTY_KEY) returns

    def delete(self, name: str) -> int:
        self.ops.append(("delete", name))
        return 1 if self.keys.pop(name, None) is not None else 0

    def sadd(self, name: str, *members: str) -> int:            # VARARGS — _rebuild_once fans in a whole chunk
        self.ops.append(("sadd", name, len(members)))
        self.keys.setdefault(name, set()).update(members)
        return len(members)

    def exists(self, name: str) -> int:
        self.ops.append(("exists", name))
        return 1 if name in self.keys else 0

    def rename(self, src: str, dst: str) -> None:
        self.ops.append(("rename", src, dst))
        if src not in self.keys:
            from redis.exceptions import ResponseError
            raise ResponseError("no such key")  # real Redis behaviour: keeps the exists→rename
        self.keys[dst] = self.keys.pop(src)     # ordering honest

    def get(self, name: str) -> Any:
        return self.dirty


@contextmanager
def use_resolve_cache(fake: Any) -> Iterator[None]:
    """Patch the resolver service's `resolve_cache` with a TenantResolveCache backed by
    `fake` (DI — no monkeypatching of the global caches registry). The service facade is
    where the resolve path reads/writes the cache, so patch it there."""
    import tenants.resolver.service as _svc
    from tenants.resolver import TenantResolveCache

    rc = TenantResolveCache(cache=fake)
    with mock.patch.object(_svc, "resolve_cache", rc):
        yield rc


def make_tenant(status: str | None = None, schema_name: str = "alpha",
                shard_alias: str = "shard_a") -> Any:
    status = status or Tenant.Status.ACTIVE
    t = Tenant(id=5, schema_name=schema_name, company_name="Alpha", status=status)
    t.shard = Shard(id=2, alias=shard_alias, name="A")
    return t


def make_domain_model(tenant: Any) -> type:
    """Fake Domain model. `.objects.select_related(...).get(domain=...)` returns a row
    whose `.tenant` is `tenant` when `tenant` is given (a hit), else raises
    DoesNotExist (a miss). Counts DB `.get()` calls in `.db_calls`."""
    class DoesNotExist(Exception):
        pass

    calls = {"n": 0}

    class _Objects:
        @classmethod
        def select_related(cls, *a: Any) -> type:
            return cls

        @classmethod
        def get(cls, domain: str | None = None) -> Any:
            calls["n"] += 1
            if tenant is not None:
                row = type("Row", (), {})()
                row.tenant = tenant
                return row
            raise DoesNotExist()

    class FakeDomain:
        pass

    FakeDomain.DoesNotExist = DoesNotExist
    FakeDomain.objects = _Objects
    FakeDomain.db_calls = calls
    return FakeDomain


class FakeLock:
    """Stand-in for redis-py's Lock: records acquire/release; release() can raise LockError
    to simulate a lock that expired while the holder was still working (no longer ours).

    Shared by the two fenced locks in this project — the resolver's warm lock and the beat
    fanout wave lock — so both are exercised against the same stand-in."""

    def __init__(self, acquired: bool = True, release_raises: bool = False,
                 release_error: Exception | None = None) -> None:
        self._acquired = acquired
        self._release_raises = release_raises       # LockError: the key is no longer ours
        self._release_error = release_error         # anything else release() can raise
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, blocking: bool = True, **kw: Any) -> bool:
        self.acquire_calls += 1
        return self._acquired

    def release(self) -> None:
        self.release_calls += 1
        if self._release_raises:
            from redis.exceptions import LockError
            raise LockError("not owned")
        if self._release_error is not None:
            raise self._release_error


class FakeLockRedis:
    """Returns a preset FakeLock from .lock(); records the lock() call args."""

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self.lock_calls = []

    def lock(self, name: str, timeout: float | None = None, **kw: Any) -> Any:
        self.lock_calls.append((name, timeout))
        return self._lock
