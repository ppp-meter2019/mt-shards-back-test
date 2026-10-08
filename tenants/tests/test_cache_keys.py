"""Tenant-scoped Redis key contract (commons.platform.cache_keys + CACHES wiring).

DB-free and Redis-free (SimpleTestCase): the active schema is faked by patching the module's
`connection`, the established idiom in this suite (see test_celery_compat.py). Testing the
real isolation would need a live Redis, and the whole CI suite is deliberately service-free —
so what is pinned here is the KEY SHAPE, which is the thing another service has to agree with.

These tests only run under USE_MULTITENANT=True (the `tenants` app must be installed for its
suite to be discovered), which is the mode the contract applies to. The one standalone
invariant that matters — that the base does NOT tenant-scope `default` — is checked at source
level, because this process cannot be in both modes at once.

Contract: deploy/redis_keys_design.md.
"""
import contextlib
import threading
import types
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

import tenants.context as tctx
from commons.platform import cache_keys

from ._settings_ast import (BASE_SETTINGS, assert_literal_assignment,
                            assert_module_constant, assert_no_dict_key)


@contextlib.contextmanager
def _as_schema(name, *, alias: str = "tenant_1", default_schema: str = "public"):
    """Fake the active shard AND its schema.

    Patches `connections` and `active_alias` together, and deliberately leaves `default` on a
    DIFFERENT schema — that is the shape of a real Celery task, where tenants.context._switch
    has set the schema on the shard connection and never touched `default`. Faking only the
    schema (the previous version of this helper patched a single `connection` object) made the
    whole question of WHICH connection is read invisible to these tests, which is exactly how
    a source bug survived them.
    """
    conns = {
        alias: types.SimpleNamespace(schema_name=name),
        "default": types.SimpleNamespace(schema_name=default_schema),
    }
    # Patches tenants.context, which now OWNS the read; cache_keys only consumes the answer
    # (through commons.platform.tenancy). Patching it there keeps these tests about the key
    # SHAPE while tenants/tests/test_context.py covers the read itself.
    with mock.patch.object(tctx, "connections", conns), \
         mock.patch.object(tctx, "bound_alias", lambda: alias):
        yield


class CacheKeyShapeTests(SimpleTestCase):
    """The shape is the cross-service contract: `tenant:<schema>:...`, leading literal first.

    The literal is what makes `tenant:<schema>:*` a single glob over one tenant's entire
    Redis footprint — per-tenant flush on tenant deletion and per-tenant memory accounting
    both depend on it. Upstream's django_tenants.cache.make_key puts the schema first but has
    no shared literal, which is why it is not used (deploy/UPSTREAM_FORK.md).
    """

    def test_make_key_shape(self) -> None:
        with _as_schema("acme_transport"):
            self.assertEqual(
                cache_keys.make_key("sess:abc", "app", 1),
                "tenant:acme_transport:app:1:sess:abc",
            )

    def test_make_key_starts_with_the_shared_literal(self) -> None:
        """The literal is the CROSS-SERVICE part of the contract, so it is pinned as a
        property over varying inputs rather than as one example — and against a HARDCODED
        string, not against TENANT_NAMESPACE. Reading the constant from the module under test
        would let a rename pass here while silently breaking every other service that globs
        `tenant:*`."""
        for schema, prefix, version in (("acme_transport", "app", 1),
                                        ("a", "", 0),
                                        ("x_9", "sess", 42)):
            with self.subTest(schema=schema, prefix=prefix, version=version), \
                 _as_schema(schema):
                key = cache_keys.make_key("k", prefix, version)
                self.assertTrue(key.startswith("tenant:"), key)
                self.assertEqual(key.split(":", 2)[1], schema)

    def test_reverse_key_round_trips(self) -> None:
        """django-redis needs the inverse for keys()/iter_keys() (delete_pattern does not
        use it).

        What this actually covers is the BOUNDED split: colons in the LOGICAL key are safe by
        construction, since `split(":", 4)` keeps everything after the fourth one. It does NOT
        cover the segment count, which depends on KEY_PREFIX staying colon-free — a colon
        there shifts every result silently. That is a deployment discipline, not a check:
        deliberately left unenforced, so do not read this test as protecting it."""
        for logical in ("k", "sess:abc", "a:b:c:d:e"):
            with self.subTest(key=logical), _as_schema("acme_transport"):
                physical = cache_keys.make_key(logical, "app", 1)
                self.assertEqual(cache_keys.reverse_key(physical), logical)

    def test_tenant_key_shape(self) -> None:
        """Prefix, never suffix. RGKB section 28 and IT-19819 propose
        `coordinates_package:{tenant_id}` — that form defeats the glob (`tenant:<schema>:*`
        stops finding the key), and with it per-tenant flush and per-tenant memory accounting.
        This exact-equality assertion is what fails if someone re-aligns the code with that
        wording instead of raising the ticket."""
        with _as_schema("acme_transport"):
            self.assertEqual(
                cache_keys.tenant_key("coordinates_package"),
                "tenant:acme_transport:coordinates_package",
            )


class CacheKeyContextTests(SimpleTestCase):
    """The two contracts differ ON PURPOSE, and this is the pair of tests that says so."""

    def test_tenant_key_refuses_the_public_schema(self) -> None:
        """A manual key has no framework around it: built off-tenant it would just quietly
        address another namespace. Same stance as the router's PUBLIC_MODEL_GUARD='raise'."""
        with _as_schema("public"):
            with self.assertRaises(ImproperlyConfigured):
                cache_keys.tenant_key("coordinates_package")

    def test_make_key_allows_the_public_schema(self) -> None:
        """Public-host admin sessions legitimately live under `tenant:public:`. A cache is
        not the place to enforce tenancy, so KEY_FUNCTION must not raise here."""
        with _as_schema("public"):
            self.assertEqual(
                cache_keys.make_key("sess:abc", "app", 1),
                "tenant:public:app:1:sess:abc",
            )

    def test_schema_comes_from_the_active_shard_not_default(self) -> None:
        """THE regression. tenants.context._switch() sets the schema on connections[<shard>],
        so in a Celery task `default` is still public — and business tenants are always on a
        shard (Tenant.clean forbids the default one). Reading `default` would collapse every
        tenant into `tenant:public:` and make tenant_key() refuse from inside a live tenant
        context. One source for both: tenants.context.current_schema_name, which
        tenants.celery.compat re-exports (see tenants/tests/test_context.py)."""
        with _as_schema("acme", alias="tenant_1", default_schema="public"):
            self.assertEqual(cache_keys.make_key("k", "app", 1), "tenant:acme:app:1:k")
            self.assertEqual(cache_keys.tenant_key("coordinates_package"),
                             "tenant:acme:coordinates_package")

    def test_stale_default_connection_is_not_inherited(self) -> None:
        """THE leak this reader exists to avoid. django_tenants resets the schema at the
        START of process_request and never on close, so BETWEEN requests
        connections['default'] still holds the tenant just served. With nothing bound that
        must read as public — not as that tenant. Verified by hand against the previous
        implementation (which read active_alias(), coalescing to 'default'): tenant_key()
        returned `tenant:acme:coordinates_package` and did NOT raise."""
        stale = {"default": types.SimpleNamespace(schema_name="acme")}
        with mock.patch.object(tctx, "connections", stale), \
             mock.patch.object(tctx, "bound_alias", lambda: None):
            self.assertEqual(cache_keys.make_key("k", "app", 1), "tenant:public:app:1:k")
            with self.assertRaises(ImproperlyConfigured):
                cache_keys.tenant_key("coordinates_package")


class CacheKeyThreadTests(SimpleTestCase):
    """What a CHILD THREAD gets, pinned because it is a cross-tenant hazard and because the
    docstring here used to claim the opposite ("a containment failure, never a leak").

    Two independent mechanisms both point at `public`, so there is no fallback path:
      * `current_db` is a ContextVar and `threading.Thread` starts with a fresh context, so
        `bound_alias()` is None in the child;
      * `django.db.connections` is thread-local, so even a carried-over context would find a
        connection with no schema set on it.

    These tests use the REAL ContextVar via `use_alias`, not a patched `bound_alias` — a
    lambda would return the same value in any thread and would prove nothing.
    """

    @staticmethod
    def _in_thread(fn):
        out = {}
        def run():
            try:
                out["value"] = fn()
            except Exception as exc:          # noqa: BLE001 — recorded, re-raised by caller
                out["exc"] = exc
        t = threading.Thread(target=run)
        t.start()
        t.join()
        return out

    @staticmethod
    @contextlib.contextmanager
    def _fake_shards():
        conns = {
            "t1": types.SimpleNamespace(schema_name="alpha"),
            "t2": types.SimpleNamespace(schema_name="beta"),
            "default": types.SimpleNamespace(schema_name="public"),
        }
        with mock.patch.object(tctx, "connections", conns):
            yield

    def test_two_tenants_collide_on_one_namespace_in_a_thread(self) -> None:
        """THE leak. Not "each unbound caller gets a broken namespace of its own" — they all
        get the SAME one, so two threads serving different tenants read each other's keys."""
        from tenants.context import use_alias
        keys = []
        with self._fake_shards():
            for alias in ("t1", "t2"):
                with use_alias(alias):
                    keys.append(self._in_thread(
                        lambda: cache_keys.make_key("orders:open", "app", 1))["value"])
        self.assertEqual(keys[0], keys[1], "the collision is the point of this test")
        self.assertEqual(keys[0], "tenant:public:app:1:orders:open")

    def test_carrying_the_context_over_is_what_separates_them(self) -> None:
        """Sensitivity check: the collision above is caused by the ContextVar, not by some
        incidental property of the fake connections. Capture the context in the PARENT and the
        two tenants separate again.

        Also pins the trap in that fix: `copy_context()` must run in the parent. Called inside
        the thread target it copies the child's already-empty context and changes nothing —
        a no-op that reads like a fix.
        """
        import contextvars
        from tenants.context import use_alias

        def key_from_thread(alias, *, capture_in_parent):
            with self._fake_shards(), use_alias(alias):
                out = {}
                fn = lambda: out.setdefault(                      # noqa: E731
                    "k", cache_keys.make_key("orders:open", "app", 1))
                if capture_in_parent:
                    ctx = contextvars.copy_context()
                    target = lambda: ctx.run(fn)                  # noqa: E731
                else:
                    target = lambda: contextvars.copy_context().run(fn)   # noqa: E731
                t = threading.Thread(target=target)
                t.start()
                t.join()
                return out["k"]

        self.assertEqual(key_from_thread("t1", capture_in_parent=True),
                         "tenant:alpha:app:1:orders:open")
        self.assertEqual(key_from_thread("t2", capture_in_parent=True),
                         "tenant:beta:app:1:orders:open")
        # the no-op form: both tenants still collide
        self.assertEqual(key_from_thread("t1", capture_in_parent=False),
                         key_from_thread("t2", capture_in_parent=False))

    def test_make_key_degrades_silently_but_tenant_key_refuses(self) -> None:
        """The asymmetry saves exactly half: the manual-key path is protected, the cache API
        is not — and cannot be, because make_key is a KEY_FUNCTION and must stay total."""
        from tenants.context import use_alias
        with self._fake_shards(), use_alias("t1"):
            self.assertEqual(cache_keys.make_key("k", "app", 1), "tenant:alpha:app:1:k")
            self.assertEqual(
                self._in_thread(lambda: cache_keys.make_key("k", "app", 1))["value"],
                "tenant:public:app:1:k",
            )
            self.assertIsInstance(
                self._in_thread(lambda: cache_keys.tenant_key("coords"))["exc"],
                ImproperlyConfigured,
            )


class CachesWiringTests(SimpleTestCase):
    """WHICH aliases are tenant-scoped. Each assertion here is a real failure mode."""

    KEY_FN = "commons.platform.cache_keys.make_key"
    REVERSE_FN = "commons.platform.cache_keys.reverse_key"

    def test_default_is_tenant_scoped(self) -> None:
        self.assertEqual(settings.CACHES["default"].get("KEY_FUNCTION"), self.KEY_FN)
        self.assertEqual(settings.CACHES["default"].get("REVERSE_KEY_FUNCTION"),
                         self.REVERSE_FN)

    def test_reverse_key_function_is_present_whenever_key_function_is(self) -> None:
        """django-redis needs both: with KEY_FUNCTION alone, keys() and iter_keys() fall back
        to default_reverse_key (`split(":", 2)[2]`), which on a five-segment key returns
        `<prefix>:<version>:<key>` — a mangled logical key rather than a failure, which is
        worse.

        delete_pattern() is NOT in that set, contrary to what this docstring used to say: it
        SCANs and deletes, returning a count, and never reverses a key. See the same
        correction on cache_keys.reverse_key, and test_reverse_key_round_trips above, which
        has had it right all along."""
        for alias, conf in settings.CACHES.items():
            with self.subTest(alias=alias):
                if conf.get("KEY_FUNCTION"):
                    self.assertTrue(conf.get("REVERSE_KEY_FUNCTION"))

    def test_only_default_is_tenant_scoped(self) -> None:
        """Every other alias is unscoped for a reason of its own, and each reason is a real
        failure mode rather than an omission:

        sessions       the backing store is SHARED — django.contrib.sessions is in
                       SHARED_APPS only, so `django_session` lives in the public schema and
                       nowhere else. Scoping the cache per schema would partition it
                       differently from the table it caches, and since cached_db.load()
                       returns a cache hit WITHOUT consulting the DB while cached_db.delete()
                       clears only the CURRENT schema's key, invalidation would be narrower
                       than the thing invalidated.
        tenant_resolve resolution runs BEFORE the schema is known, so every entry would be
                       stamped `public` and never hit. Second reason: tenants.resolver.cache
                       owns that alias's PHYSICAL layout (`_snapshot_key_prefix`,
                       '<KEY_PREFIX>:<version>:') for its raw SCANs in the registry.
        beat_lock      set and checked in the public-context dispatcher; a per-tenant key
                       would leave the overlap lock unable to see its own entry.
        """
        for alias, conf in settings.CACHES.items():
            if alias == "default":
                continue
            with self.subTest(alias=alias):
                self.assertIsNone(conf.get("KEY_FUNCTION"))
                self.assertIsNone(conf.get("REVERSE_KEY_FUNCTION"))

    def test_the_alias_set_is_exactly_this(self) -> None:
        """Pins WHICH aliases exist, not just how the existing ones are configured.

        The loop above iterates settings.CACHES, so it can only ever assert over aliases that
        are present — deleting one can never fail it. That gap is not theoretical: a `system`
        alias was proposed, rejected, removed, and its description survived in the docstring
        above for a whole changeset while every test stayed green."""
        self.assertEqual(set(settings.CACHES),
                         {"default", "sessions", "tenant_resolve", "beat_lock"})

    def test_sessions_have_their_own_unscoped_alias(self) -> None:
        """Pinned separately from the loop above: the loop would still pass if
        SESSION_CACHE_ALIAS pointed back at `default`, which is exactly the regression."""
        self.assertEqual(settings.SESSION_CACHE_ALIAS, "sessions")
        self.assertIn("sessions", settings.CACHES)
        self.assertIsNone(settings.CACHES["sessions"].get("KEY_FUNCTION"))

    def test_default_fails_loud_while_sessions_fail_open(self) -> None:
        """The two aliases sit on one instance but answer a Redis outage differently, and
        both answers are deliberate. `default` raises, so a misconfigured LOCATION cannot
        masquerade as a permanent cache miss. `sessions` swallows, because
        cached_db.delete() is NOT wrapped in the session backend's own try/except — with it
        raising, logging out during an outage would 500."""
        self.assertIs(settings.CACHES["default"]["OPTIONS"]["IGNORE_EXCEPTIONS"], False)
        self.assertIs(settings.CACHES["sessions"]["OPTIONS"]["IGNORE_EXCEPTIONS"], True)

    def test_standalone_base_keeps_sessions_on_default(self) -> None:
        """Source-level (this process is MT). Standalone has no KEY_FUNCTION, so there is
        nothing for a separate session alias to protect against — the whole reason sessions
        move in MT is that `default` becomes tenant-scoped there."""
        assert_module_constant(self, "SESSION_CACHE_ALIAS", "default", BASE_SETTINGS)

    def test_caches_are_rebuilt_not_merged_into_the_base(self) -> None:
        """settings_multitenant.py must BUILD CACHES, never spread the base dict — same rule
        as DATABASES, and for the same reason: the base is the standalone host project's
        config, and multi-tenant must not inherit whatever instance or options it names.
        Source-level, because the resolved setting cannot tell a literal from a merge."""
        assert_literal_assignment(self, "CACHES")

    def test_standalone_base_does_not_tenant_scope_the_default_cache(self) -> None:
        """Source-level: this process is in MT, so the standalone value cannot be read from
        settings. Standalone has ONE tenant — prefixing every key with `tenant:public:` would
        be pure noise in the host project's Redis, and the host project is what inherits the
        base."""
        for forbidden in ("KEY_FUNCTION", "REVERSE_KEY_FUNCTION"):
            with self.subTest(option=forbidden):
                assert_no_dict_key(self, "CACHES", forbidden, BASE_SETTINGS)
