"""TenantTask.__call__ per-invocation schema switch + the worker-pool guardrail (MT-only).
DB-free: the tenant branch mocks get_tenant_for_schema / tenant_context. See app.py / task.py.
"""
from contextlib import contextmanager
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from tenants_back.celery import app
from tenants.context import active_alias, current_db, use_alias


@app.task(bind=True, name="tests.tenanttask_probe")
def _probe(self):
    """Report the EFFECTIVE db the task body would route to (None→default)."""
    return active_alias()


class TenantTaskCallTests(SimpleTestCase):
    def test_public_header_pins_default(self):
        """A task whose message carries the public schema runs pinned to 'default' — it does
        NOT trust ambient current_db (the R1 amplifier fix)."""
        with use_alias("leftover_shard"):                 # simulate a leaked ambient alias
            r = _probe.apply(headers={"_schema_name": "public"})
        self.assertEqual(r.result, "default")

    def test_bare_call_inherits_ambient(self):
        """A bare in-process call (no request) inherits the caller's context — unchanged from
        the old prerun/postrun behavior (those signals don't fire off the worker/eager path)."""
        with use_alias("shard_x"):
            self.assertEqual(_probe(), "shard_x")

    def test_bare_call_without_ambient_is_default(self):
        self.assertEqual(_probe(), "default")

    def test_tenant_header_enters_tenant_context(self):
        """A tenant-scoped message enters tenant_context(tenant) for the task body."""
        @contextmanager
        def fake_tenant_context(tenant):
            tok = current_db.set("shard_acme")
            try:
                yield
            finally:
                current_db.reset(tok)

        with mock.patch("tenants.celery.task.TenantTask.get_tenant_for_schema",
                        return_value=object()), \
             mock.patch("tenants.celery.task.tenant_context", fake_tenant_context):
            r = _probe.apply(headers={"_schema_name": "acme"})
        self.assertEqual(r.result, "shard_acme")

    def test_context_restored_after_exception(self):
        """The with-block's finally restores current_db even when the task body raises."""
        @app.task(bind=True, name="tests.tenanttask_boom")
        def _boom(self):
            raise RuntimeError("boom")

        r = _boom.apply(headers={"_schema_name": "public"})
        self.assertTrue(r.failed())
        self.assertIsNone(current_db.get())               # not stuck on a leaked alias (unset)


class CeleryPoolGuardrailTests(SimpleTestCase):
    def test_raises_on_cooperative_pool(self):
        from tenants.celery.app import _guard_worker_pool
        for pool in ("gevent", "eventlet"):
            with self.assertRaises(ImproperlyConfigured):
                _guard_worker_pool(options={"pool": pool})

    def test_allows_noncooperative_pools(self):
        from tenants.celery.app import _guard_worker_pool
        for pool in ("prefork", "threads", "solo", None):
            self.assertIsNone(_guard_worker_pool(options={"pool": pool}))


class _FakeL1:
    def __init__(self):
        self.store = {}

    def get(self, key, default):
        return self.store.get(key, default)

    def set(self, key, value, expire_seconds=None):
        self.store[key] = value


class _FakeGlobal:
    """Stand-in for tenants.resolver.resolve_cache with the schema-snap read surface."""
    HOLD = object()
    MISS = object()

    def __init__(self, enabled=True, snapshot=None):
        self.enabled = enabled
        self._snapshot = snapshot            # a POSITIVE tenant | self.HOLD | self.MISS
        self.put_schema_calls = []

    def get_schema_snapshot(self, schema):
        return self._snapshot

    def put_schema(self, schema, tenant):
        self.put_schema_calls.append(schema)


class GetTenantForSchemaTests(SimpleTestCase):
    """The worker resolution tiering: GLOBAL (invalidated) -> LOCAL -> DB."""

    def _run(self, glob, l1_seed=None, db_tenant="DB"):
        from tenants.celery import task as taskmod
        l1 = _FakeL1()
        if l1_seed is not None:
            l1.store["s"] = l1_seed
        db = mock.Mock()
        db.objects.select_related.return_value.get.return_value = db_tenant
        with mock.patch("tenants.resolver.resolve_cache", glob), \
             mock.patch.object(taskmod.TenantTask, "tenant_cache", classmethod(lambda cls: l1)), \
             mock.patch.object(taskmod.TenantTask, "_l1_seconds", classmethod(lambda cls: 60)), \
             mock.patch("tenants.models.Tenant", db):
            result = taskmod.TenantTask.get_tenant_for_schema("s")
        return result, l1, db

    def test_global_positive_hit_skips_db_and_leaves_l1_untouched(self):
        glob = _FakeGlobal(snapshot="SNAP")
        result, l1, db = self._run(glob)
        self.assertEqual(result, "SNAP")
        db.objects.select_related.assert_not_called()
        self.assertNotIn("s", l1.store)                        # global hit does NOT fill L1

    def test_global_hold_bypasses_stale_local_goes_db(self):
        glob = _FakeGlobal(snapshot=_FakeGlobal.HOLD)
        result, l1, db = self._run(glob, l1_seed="STALE")
        self.assertEqual(result, "DB")                         # NOT the stale L1 value
        db.objects.select_related.assert_called_once()

    def test_global_miss_falls_to_local_hit(self):
        glob = _FakeGlobal(snapshot=_FakeGlobal.MISS)
        result, l1, db = self._run(glob, l1_seed="L1HIT")
        self.assertEqual(result, "L1HIT")
        db.objects.select_related.assert_not_called()

    def test_global_miss_local_miss_hits_db_l1_only(self):
        glob = _FakeGlobal(snapshot=_FakeGlobal.MISS)
        result, l1, db = self._run(glob)
        self.assertEqual(result, "DB")
        self.assertEqual(l1.store["s"], "DB")                  # L1 filled
        self.assertEqual(glob.put_schema_calls, [])            # worker NEVER writes the global cache

    def test_global_disabled_uses_local_then_db_without_global_write(self):
        glob = _FakeGlobal(enabled=False)
        result, l1, db = self._run(glob)
        self.assertEqual(result, "DB")
        self.assertEqual(glob.put_schema_calls, [])            # no global write when disabled


class SimpleCacheTests(SimpleTestCase):
    """L1 (per-worker) cache: expiry eviction on get + opportunistic purge on set (R4)."""

    def test_expired_is_evicted_on_get(self):
        from tenants.celery.cache import SimpleCache
        store = {}
        c = SimpleCache(storage=store)
        c.set("k", "v", expire_seconds=-1)                     # already expired
        self.assertEqual(c.get("k", "MISS"), "MISS")
        self.assertNotIn("k", store)                           # evicted, not just skipped

    def test_opportunistic_purge_on_set(self):
        from datetime import datetime, timedelta, timezone
        from tenants.celery.cache import SimpleCache, _CacheEntry
        store = {}
        c = SimpleCache(storage=store)
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        for i in range(SimpleCache._PURGE_AT):                 # expired entries never get()-ed again
            store[f"old{i}"] = _CacheEntry(f"old{i}", i, past)
        c.set("new", "v", expire_seconds=60)                   # len >= _PURGE_AT → sweep expired
        self.assertNotIn("old0", store)
        self.assertIn("new", store)
        self.assertEqual(len(store), 1)
