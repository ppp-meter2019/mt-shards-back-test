"""Tenant-scoped Redis key shape — the ONE place the tenant token and the key layout live.

Every tenant-scoped key in Redis, whatever writes it, looks like:

    tenant:<schema>:<rest>

The leading literal matters: `tenant:<schema>:*` is then ONE glob over everything that was
minted HERE, whichever namespace or service produced it. That is what per-tenant flush on
tenant deletion and per-tenant memory accounting need.

It is one glob, NOT a complete footprint: a few things sit outside it, so a deletion
procedure is a procedure and not a pattern — deploy/redis_keys_design.md section D2 has the
table and is the authoritative copy.

One of them is dangerous enough to repeat here: cache.clear() is a Redis FLUSHDB. It sits
below KEY_FUNCTION, so it is not scoped at all — called from any context it destroys every
tenant AND the sessions sharing that instance. Never call it on `default` under multi-tenant.

The literal is also why this module does not simply reuse `django_tenants.cache.make_key`
(which emits
`<schema>:<prefix>:<version>:<key>` — schema first, but no shared literal to glob on). The
deliberate non-use of that upstream helper is recorded in deploy/UPSTREAM_FORK.md.

THE TOKEN IS `schema_name`, NOT the numeric Tenant.pk. Reasons, in order of weight:
  1. The schema is available in ANY process that has a connection — a Celery worker, a
     WebSocket consumer, deep inside a service layer. `tenant.id` needs a loaded Tenant
     object (`request.tenant`) or a query, and there is no request in half of those places.
     See "Works outside WSGI" below.
  2. Tenant.schema_name is immutable in this project (create-only; tenants/models.py), so it
     is exactly as stable an identifier as the PK.
  3. `redis-cli --scan --pattern 'tenant:acme_transport:*'` is readable without consulting
     the database. `tenant:42:*` is not.
  4. The schema is ALREADY the routing axis here, with a CI guard confining its readers
     (scripts/ci_guard_schema_name.sh). A second token would be a second axis to police.
The numeric tenant_id stays the token for the S3 / data-lake side
(routes/services/service_offline_coordinates_s3_uploader.py), where the prefix repeats in
every object key and every Hive partition path and its length is a real cost. In Redis the
aggregates are HASHES — one key per tenant per structure, ~2800 keys at 700 schemas — so the
prefix length is noise. Two different problems, two different answers, on purpose.

TWO CONTRACTS, and the difference is the point:
  * `make_key()` (the Django cache KEY_FUNCTION) must NOT raise on the public schema.
    Public-host admin sessions legitimately live under `tenant:public:`.
  * `tenant_key()` (manual redis-py keys) MUST raise there. A manual key built with no
    tenant context would silently read or write another namespace — the same failure the
    router refuses with PUBLIC_MODEL_GUARD = "raise". Callers that legitimately have no
    tenant are not stuck: the Django cache API still works on the public schema (make_key
    yields `tenant:public:…`, a valid namespace of its own), and anything with its own
    lifetime or eviction policy gets a PURPOSE-NAMED alias, the way `tenant_resolve` and
    `beat_lock` did. What there deliberately is NOT is a generic "system" bucket — an alias
    whose failure mode and eviction policy nobody had to think about is how cross-tenant
    leaks get parked.

USING tenant_key() — most callers should NOT. CACHES['default'] is already tenant-scoped
through make_key, so ordinary caching needs nothing from here:

    cache.set("orders:open_count", n, timeout=60)   # -> tenant:acme:app:1:orders:open_count

tenant_key() is for what the cache API cannot express: hashes, sets, SET NX/EX, INCR, EXPIRE,
pipelines, SCAN — i.e. the four aggregate structures of RGKB section 28 and the VTL keys.
Pair it with commons.platform.redis_client.tenant_raw_client():

    COORDS = "coordinates_package"          # a module constant; only the SUFFIX is constant

    def record_vehicle_position(vehicle_id: int, payload: dict) -> None:
        key = tenant_key(COORDS)            # tenant:acme:coordinates_package
        try:
            tenant_raw_client().hset(key, str(vehicle_id), json.dumps(payload))
        except RedisError:                  # the raw client PROPAGATES; decide here
            logger.warning("coordinates: dropped a sample", exc_info=True)

    def drain_coordinates() -> dict:
        key = tenant_key(COORDS)
        with tenant_raw_client().pipeline() as pipe:
            pipe.hgetall(key)
            pipe.delete(key)
            raw, _ = pipe.execute()
        return {k.decode(): json.loads(v) for k, v in raw.items()}

Three ways to get it wrong, all silent:
  * a module-level `KEY = tenant_key(...)` — evaluated at import, where there is no context
    (raises), and if it somehow passes it pins one tenant for the life of the process. Build
    the key per call; keep only the suffix constant.
  * a part containing ":" — this is a join, so `tenant_key("vtl", "a:b")` is indistinguishable
    from `tenant_key("vtl", "a", "b")`. Normalise or hash anything that might carry one.
  * `f"tenant:{connection.schema_name}:..."` by hand — bypasses the context check AND reads
    the wrong connection (always `default`, whatever is bound; see current_schema below).

To sweep a tenant, do not parse keys — match them: `SCAN MATCH 'tenant:<schema>:*'`, plus the
`sess:*` namespace separately (see the table in deploy/redis_keys_design.md section D2).

WORKS OUTSIDE WSGI. Nothing here touches `request`, middleware state or a thread-local set
on the HTTP path: the only input is the schema on the BOUND connection —
`connections[bound_alias()].schema_name`, never the AMBIENT `django.db.connection`, and
never at all when no context is bound (see current_schema below). Note that bound alias is
routinely "default" — that is the public / management path, which both
ShardAwareTenantMiddleware and TenantTask pin with `use_alias("default")` — so
`connections["default"]` in a traceback is normal here. What the rule forbids is reading a
connection that nobody bound, not reading that particular one. A rewritten WebSocket
service (Django Channels — a SEPARATE ASGI deployment, see the ASGI_APPLICATION note in
settings_base.py) and the Celery workers must be able to build the same keys as the API.
That constraint is why the token is read from the connection and passed nowhere.

See deploy/redis_keys_design.md for the full contract, and the open question of who owns the
broadcast of the four aggregate structures.
"""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections

# The shared leading literal. Changing it invalidates every tenant-scoped key in Redis
# (a cold cache) AND breaks the glob contract with any other service writing these keys.
TENANT_NAMESPACE = "tenant"

# Both come from the mode facade, NOT from django_tenants / tenants: in standalone the first
# is not installed, and importing the second from outside `tenants` would break the app
# boundary of deploy/standalone_multitenant_design.md section 4. The facade returns the
# literal "public" and the alias "default" there.
from commons.platform.tenancy import bound_alias, get_public_schema_name  # noqa: E402

if settings.USE_MULTITENANT:

    def current_schema() -> str:
        """The schema of the BOUND routing context, or public when nothing is bound.

        Read from the bound SHARD connection, never from `default`: tenants.context._switch()
        applies the schema to connections[<shard alias>], so in a Celery task `default` is
        never touched. Business tenants are always on a shard (Tenant.clean forbids the
        default one), so that is the normal path, not an edge case.

        bound_alias(), NOT active_alias(). The latter coalesces "no context" into "default",
        and that collapse is a cross-tenant hazard HERE specifically: django_tenants resets
        the schema at the START of process_request and never on close, so BETWEEN requests
        connections["default"] still holds the tenant just served. An off-request caller
        would mint keys in that tenant's namespace, silently — exactly what tenant_key()
        promises to refuse. The check has to stand on whether a context EXISTS, not on what
        the schema happens to say.

        Falling back to public rather than raising removes the PRACTICAL reason to raise: this
        feeds the KEY_FUNCTION of CACHES['default'], it runs on every cache call, and an
        exception here reaches the caller — django_redis's omit_exception intercepts
        ConnectionInterrupted alone, so a non-redis exception is never absorbed whatever
        IGNORE_EXCEPTIONS says (it is False on this alias anyway). tenant_key() turns that
        same public result into a refusal, which is the contract that has to be strict.

        It does NOT make make_key TOTAL, as an earlier version of this docstring claimed:
        `connections[alias]` raises ConnectionDoesNotExist for an alias outside DATABASES,
        straight out of the KEY_FUNCTION and into code with no tenancy in it. Only use_alias()
        can bind such an alias — it sets current_db without touching `connections`, having no
        schema to apply and so nothing to resolve — whereas _switch() (tenant_context /
        schema_context) resolves the connection on its first line and fails at the `with`,
        before any key is minted. Both use_alias call sites pass the literal "default", so
        this is a property of the contract rather than a reachable failure.

        THAT FALLBACK CAN LEAK ACROSS TENANTS, and an earlier version of this docstring said
        the opposite ("a containment failure, never a leak"). Unbound callers do not each get
        a broken namespace of their own — they all get the SAME one, `tenant:public:`. Two
        unbound callers serving different tenants therefore read and write each other's
        entries. Verified: under `use_alias("t1")` (schema alpha) and `use_alias("t2")`
        (schema beta), a child thread in either produces the identical key
        `tenant:public:app:1:orders:open`.

        A thread is the realistic way to end up unbound: `current_db` is a ContextVar and
        `threading.Thread` starts with a fresh context, so `bound_alias()` is None there. A
        second mechanism points the same way even if the context were carried over —
        `django.db.connections` is thread-local, so the connection a child thread sees has no
        schema set on it either. tenant_key() raises in that situation; make_key cannot, which
        is why the hazard lives entirely on the cache-API side. Pinned by
        test_cache_keys.CacheKeyThreadTests. Do not cache tenant data from a thread pool.

        `or get_public_schema_name()` is belt-and-braces for a hand-rolled set_schema or a
        FakeTenant — NOT for a fresh connection: the backend's __init__ ends with
        set_schema_to_public(), so schema_name is never falsy there.

        The read is MT-only — hence the branch, and hence this file's entry in the ALLOW
        list of scripts/ci_guard_schema_name.sh.
        """
        alias = bound_alias()
        if alias is None:
            return get_public_schema_name()
        return connections[alias].schema_name or get_public_schema_name()

    def tenant_token() -> str:
        """The token for MANUAL keys. Raises outside a tenant context.

        A manual key is not covered by any cache-framework machinery: if it were built on
        the public schema it would just quietly point at the wrong namespace. Fail instead.
        """
        schema = current_schema()
        # This one comparison covers BOTH the real public schema and the unset state:
        # current_schema() normalises a falsy schema_name to public (see its docstring), so
        # there is no separate falsy branch to keep in sync.
        #
        # The comparison goes through the MODE FACADE (pattern (b) in the ALLOW note of
        # scripts/ci_guard_schema_name.sh): commons.platform.tenancy returns the literal
        # "public" in standalone without importing django_tenants, which is not installed
        # there. Safe regardless, since this whole branch is MT-only.
        if schema == get_public_schema_name():
            raise ImproperlyConfigured(
                "tenant_key() called outside a tenant context (schema is public). Either "
                "build the key inside schema_context()/tenant_context(), or — if the data "
                "genuinely has no tenant — use the Django cache API (which namespaces it "
                "under tenant:public:) or give it its OWN cache alias, the way "
                "tenant_resolve and beat_lock do (see deploy/redis_keys_design.md)."
            )
        return schema

    def make_key(key, key_prefix, version) -> str:
        """CACHES['default']['KEY_FUNCTION'].

        Does NOT raise on public: sessions of the public-host admin belong under
        `tenant:public:`, and a cache is not the place to enforce tenancy.
        """
        return f"{TENANT_NAMESPACE}:{current_schema()}:{key_prefix}:{version}:{key}"

    def reverse_key(key: str) -> str:
        """CACHES['default']['REVERSE_KEY_FUNCTION'] — required by django-redis for keys()
        and iter_keys(), the two methods that hand PHYSICAL keys back to the caller
        (client/default.py:706 and :689 — the only two call sites of self.reverse_key).

        delete_pattern() does NOT use it, though an earlier version of this line said it did:
        it builds a pattern, SCANs, deletes and returns a COUNT, so no key is ever turned
        back into a logical one. That matters for reading tenants/resolver/cache.py, whose
        snapshot sweep runs on delete_pattern and therefore does not depend on anything here.

        Inverse of make_key: five segments."""
        return key.split(":", 4)[4]

    def tenant_key(*parts: str) -> str:
        """`tenant:<schema>:<part>:<part>...` for MANUAL redis-py keys. Raises off-tenant."""
        return ":".join((TENANT_NAMESPACE, tenant_token(), *parts))

else:
    # Standalone: one tenant, nothing to separate — so the key layout is STOCK Django.
    #
    # These two are Django's OWN functions, aliased rather than reimplemented. The bodies
    # were byte-for-byte copies of them, and a copy is exactly the thing that drifts in
    # silence if either upstream ever changes its layout. Aliasing also states the
    # equivalence in code instead of asserting it in a comment.
    #
    # settings_multitenant.py is what installs KEY_FUNCTION, so in standalone these are not
    # wired into CACHES at all. They stay exported anyway: a host project may legitimately
    # set KEY_FUNCTION here for reasons of its own, and it should then get stock behaviour
    # rather than an exception.
    #
    # Importing django_redis here is safe, unlike django_tenants: the base CACHES backend is
    # django_redis.cache.RedisCache (settings_base.py), so the package is a hard dependency
    # in BOTH modes. `default_reverse_key` is undecorated and django_redis itself installs it
    # by dotted path as the default (client/default.py) — about as public as that package
    # gets, but it is still someone else's internal, so it is listed in
    # deploy/UPSTREAM_FORK.md §4.
    from django.core.cache.backends.base import default_key_func as make_key
    from django_redis.util import default_reverse_key as reverse_key

    def current_schema() -> str:
        # `public`, not "": the same string get_public_schema_name() returns here, so shared
        # business code comparing the two behaves identically in both modes. Returning ""
        # made `current_schema() == get_public_schema_name()` True under MT-without-context
        # and False here — one name, two meanings, and neither of them wrong on its own.
        return get_public_schema_name()

    def tenant_token() -> str:
        # Deliberately NOT public: in MT this function never returns the public schema, it
        # RAISES there. An empty string is the honest standalone answer — there is no tenant
        # token, and here that is not an error.
        return ""

    def tenant_key(*parts: str) -> str:
        # Nothing to alias: a "namespaced manual key" is not a cache-framework concept, so
        # Django has no equivalent. One tenant => the tenant segment is simply absent.
        return ":".join(parts)


__all__ = [
    "TENANT_NAMESPACE", "current_schema", "tenant_token",
    "make_key", "reverse_key", "tenant_key",
]
