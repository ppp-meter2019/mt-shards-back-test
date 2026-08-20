"""Shared DB-free test helpers for the tenant-resolution cache & gate.

These tests are SimpleTestCase (no DB): the DB and the Redis cache are replaced by
fakes/mocks, so they run without Postgres or Redis. Tests that genuinely need the DB
(invalidation wiring, immutable-alias clean(), the login schema-stamp) are a
separate, DB-backed follow-up harness.
"""
from contextlib import contextmanager
from unittest import mock

from tenants.models import Shard, Tenant


class FakeNxCache:
    """Minimal in-memory cache covering what get_tenant / forget_hosts use,
    including the django_redis-only nx=True flag."""

    def __init__(self):
        self.store = {}

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value, timeout=None, nx=False, **kw):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def set_many(self, mapping, timeout=None, **kw):
        self.store.update(mapping)

    def get_many(self, keys):
        return {k: self.store[k] for k in keys if k in self.store}

    def delete_many(self, keys):
        for k in keys:
            self.store.pop(k, None)

    def delete_pattern(self, pattern):   # test double: only "*" is exercised
        n = len(self.store)
        self.store.clear()
        return n


@contextmanager
def use_resolve_cache(fake):
    """Patch the resolver service's `resolve_cache` with a TenantResolveCache backed by
    `fake` (DI — no monkeypatching of the global caches registry). The service facade is
    where the resolve path reads/writes the cache, so patch it there."""
    import tenants.resolver.service as _svc
    from tenants.resolver import TenantResolveCache

    rc = TenantResolveCache(cache=fake)
    with mock.patch.object(_svc, "resolve_cache", rc):
        yield rc


def make_tenant(status=None, schema_name="alpha", shard_alias="shard_a"):
    status = status or Tenant.Status.ACTIVE
    t = Tenant(id=5, schema_name=schema_name, company_name="Alpha", status=status)
    t.shard = Shard(id=2, alias=shard_alias, name="A")
    return t


def make_domain_model(tenant):
    """Fake Domain model. `.objects.select_related(...).get(domain=...)` returns a row
    whose `.tenant` is `tenant` when `tenant` is given (a hit), else raises
    DoesNotExist (a miss). Counts DB `.get()` calls in `.db_calls`."""
    class DoesNotExist(Exception):
        pass

    calls = {"n": 0}

    class _Objects:
        @classmethod
        def select_related(cls, *a):
            return cls

        @classmethod
        def get(cls, domain=None):
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
