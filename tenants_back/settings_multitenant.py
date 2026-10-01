"""Multi-tenant settings layer.

Reassembled/overridden ON TOP of the standalone base. Loaded by settings.py ONLY when
USE_MULTITENANT, and BEFORE settings_local_multitenant.py (so production overrides still
win over both). This file holds ALL the multi-tenant-specific config - including its own
DATABASES and the Aurora TLS helpers - so the base stays a clean standalone config.

It imports the building blocks + a few base objects to augment; everything defined here
is pulled back into the settings namespace by `from .settings_multitenant import *`.
See deploy/standalone_multitenant_design.md and deploy/celery_fanout_design.md.
"""
import os

from celery.schedules import crontab
from kombu import Queue

from commons.platform.beat import scoped_schedule
# FORWARD import — settings_base never imports this module, so there is no cycle and no
# ordering contract: the base is fully executed before the first line below runs. The star
# brings in every base setting this file augments (CACHES, MIDDLEWARE, REST_FRAMEWORK,
# CELERY_BROKER_URL, CELERY_BEAT_SCHEDULE, ...) plus BASE_DIR; the second import is only
# for the underscore-prefixed app blocks, which `import *` deliberately skips.
# DATABASES is the exception: it is REPLACED below, never augmented - see that block.
from .settings_base import *  # noqa: F401,F403
from .settings_base import (
    _BUSINESS_APPS,
    _DJANGO_APPS,
    _PUBLIC_MODEL_ALLOWLIST,
    _THIRD_PARTY_APPS,
)

# ---------------------------------------------------------------------------
# Apps — reassemble the SHARED_APPS + TENANT_APPS union (django-tenants requires the
# de-duplicated union as INSTALLED_APPS).
# ---------------------------------------------------------------------------
# IMPORTANT: 'tenants' MUST come BEFORE 'django_tenants' so our management commands
# (notably migrate_schemas) override the upstream versions.
SHARED_APPS = [
    "tenants",
    "django_tenants",
    *_DJANGO_APPS,
    *_THIRD_PARTY_APPS,
    # EVERY per-tenant app, so that every FK target exists when the identity table is created
    # here. Their tables stay empty: only settings_base._PUBLIC_MODEL_ALLOWLIST may be
    # touched on this schema, and tenants.routers refuses the rest.
    *_BUSINESS_APPS,
]

# Apps that need a table in EVERY tenant schema. A deliberate SUBSET of contrib
# (only contenttypes/auth/admin) + users + business. NOT built from _DJANGO_APPS:
# sessions/messages/staticfiles/gis live on the shared side only.
TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",

    # The SAME list as in SHARED_APPS above, and the duplication is the mechanism: present in
    # both schemas, the identity table SHADOWS — `search_path = [tenant, public]` resolves it
    # to the tenant's own copy, so public's operators stay invisible from inside a tenant.
    *_BUSINESS_APPS,
]

INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]


# ---------------------------------------------------------------------------
# django-tenants: model wiring, URL confs, routers, backend, base domains.
# ---------------------------------------------------------------------------
TENANT_MODEL        = "tenants.Tenant"
TENANT_DOMAIN_MODEL = "tenants.Domain"
PUBLIC_SCHEMA_NAME  = "public"

PUBLIC_SCHEMA_URLCONF = "tenants_back.urls_public"
ROOT_URLCONF          = "tenants_back.urls_tenant"

# Our router inherits TenantSyncRouter and adds multi-DB awareness.
# NOTE: django-tenants validates DATABASE_ROUTERS by a LITERAL string check
# ('django_tenants.routers.TenantSyncRouter' in DATABASE_ROUTERS), not by
# isinstance/subclass — so subclassing alone fails with
# "DATABASE_ROUTERS setting must contain 'django_tenants.routers.TenantSyncRouter'.".
# We list our router FIRST (it fully overrides db_for_read/write + allow_migrate, so it
# always decides); the upstream name is a no-op fallback that only satisfies that check.
DATABASE_ROUTERS = [
    "tenants.routers.TenantDatabaseRouter",
    "django_tenants.routers.TenantSyncRouter",
]

# Apps whose models are genuinely shard-partitioned (business data): the router REFUSES to
# route their queries when NO routing context is set (current_db unset), instead of silently
# using the default DB (wrong shard). django.contrib contenttypes/auth/admin are TENANT apps
# too but quasi-shared, and Django queries them from contexts we don't fully control (shell,
# createsuperuser, internals) → they stay on the benign default and are NOT listed here.
# The IDENTITY app is strict too: all its query sites were audited to run under a
# routing context — request path (middleware), Celery (TenantTask), bootstrap_tenant
# (tenant_context), bootstrap_public (public schema_context), and superuser/changepassword via
# `tenant_command <cmd> --schema=<schema>`. A bare, contextless User query now raises loudly
# (the router message points at tenant_command) instead of silently hitting the wrong shard.
# Exactly _BUSINESS_APPS, identity included: every per-tenant app is shard-partitioned, so a
# contextless query to any of them is the same bug and earns the same loud refusal. A
# frozenset because the router tests membership on EVERY db_for_read (O(1), not a scan), and
# because a setting nothing may mutate at runtime is the honest type for it.
TENANT_STRICT_ROUTE_APPS = frozenset(_BUSINESS_APPS)

# Runtime-readable promotion of the merge-seam list (settings_base). Underscored at the
# source because it is a settings-build INGREDIENT; public here because the ROUTER reads it
# per call, in tenants.routers._guard_public. Immutable: nothing may rewrite it at runtime.
#
# The other half of that guard — WHICH apps it polices — is TENANT_STRICT_ROUTE_APPS above.
# One set, three router behaviours: refuse a contextless query, skip data migrations on
# public, refuse a query on public.
PUBLIC_MODEL_ALLOWLIST = frozenset(_PUBLIC_MODEL_ALLOWLIST)

# How tenants.routers._guard_public reacts to a tenant-model query aimed at the public
# schema: "raise" | "warn" | "off".
#
# "raise" is the default because the allowlist above is complete for this repo, and a silent
# empty answer from an empty table is the failure this whole arrangement exists to prevent.
# Switch to "warn" for the MEASUREMENT pass at merge — run createsuperuser / login / the
# admin against public and read the log instead of guessing what the host's identity save
# path touches — then put it back.
PUBLIC_MODEL_GUARD = "raise"

# ---------------------------------------------------------------------------
# Databases - built FROM SCRATCH, deliberately NOT derived from the base.
#
# The base `default` is the STANDALONE database, and in the host project that is its
# real single-tenant production DB. Spreading it here (the old `{**DATABASES, ...}`)
# would aim django-tenants at that database the moment USE_MULTITENANT flips on. So
# this file rebinds DATABASES outright: there is no code path by which a standalone
# DB definition can reach multi-tenant.
#
# What is left below is a DEV default on localhost and nothing else. Every real
# cluster - `default` included - and every `tenant_*` shard is declared in
# settings_local_multitenant.py. The key set of DATABASES is also the universe of
# possible shards (Shard.clean() refuses an alias that is not in it, see
# deploy/DATABASE_SETUP.md), so under MT that universe comes entirely from the local
# file.
#
# NAME differs from the base entry on purpose: a dev box that runs both modes must
# not have them land in the same local database.
# ---------------------------------------------------------------------------
# The fields that belong to the MODE rather than to the host, kept separate so a local
# settings file can SPREAD them instead of restating them.
#
# This exists because of how a deployed local file actually writes DATABASES: it ASSIGNS
# `DATABASES["<alias>"] = {...}` wholesale (see settings_local_multitenant.py.example), it
# does not update the entry below. So nothing declared inside that literal survives into
# production on its own — the backend only ever arrived there because the local file happened
# to restate the same string, in a third independent copy. Anything added to the literal
# later — DISABLE_SERVER_SIDE_CURSORS, a TEST block, another OPTIONS key — would silently
# never reach a deployed host, with no error and no failing test (CI has no local file, so
# the resolved-settings test only ever sees this literal).
#
# Re-exported by settings.py because `import *` skips underscore names; the local file
# imports it from `.settings`, and SettingsLocalContractTests pins that re-export by parsing
# the tracked .example.
#
# What is NOT here: CONN_MAX_AGE (0 is a dev value, production raises it per alias once the
# topology is known) and OPTIONS (production replaces it wholesale with _aurora_db_options()
# for the TLS settings). Both are properties of the host, not of the mode.
_MT_DB_DEFAULTS = {
    # django-tenants backend: adds the schema_name connection attribute and wraps PostGIS
    # through ORIGINAL_BACKEND. Everything downstream depends on it — routing, the router,
    # and commons.platform.cache_keys all read connection.schema_name.
    "ENGINE":             "django_tenants.postgresql_backend",
    # Inert while CONN_MAX_AGE is 0 (no reused connection to health-check), but it has to be
    # in place for the local-settings override that raises CONN_MAX_AGE, where a stale pooled
    # connection is a real failure mode.
    "CONN_HEALTH_CHECKS": True,
}

DATABASES = {
    "default": {
        **_MT_DB_DEFAULTS,
        "NAME":               "tenants_back_mt",
        "USER":               "postgres",
        "PASSWORD":           "postgres",
        "HOST":               "127.0.0.1",
        "PORT":               "5432",
        # 0 = close at the end of every request. Same reasoning as the base entry
        # (settings_base.py, "Databases"), and MT makes it sharper: the count a cluster
        # sees is backend_hosts x gunicorn_workers PER ALIAS, and MT has one alias per
        # shard. settings_local_multitenant.py raises it where the topology and Aurora
        # max_connections are known.
        "CONN_MAX_AGE":       0,
        "OPTIONS":            {"connect_timeout": 5},
    },
}
ORIGINAL_BACKEND = "django.contrib.gis.db.backends.postgis"


# ---------------------------------------------------------------------------
# Aurora TLS - OPTIONS builders for settings_local_multitenant.py.
#
# These live in the MT layer, not in the base: Aurora + RDS Proxy is the topology
# multi-tenant deploys onto, and the standalone base must carry no dependency on it.
# `import *` skips underscore-prefixed names, so settings.py re-exports both
# EXPLICITLY in its multi-tenant branch - see the dispatcher's docstring.
# ---------------------------------------------------------------------------

# AWS RDS Certificate Authority bundle, used by psycopg's sslrootcert to verify
# Aurora's TLS certificate. The file is vendored in the repo at deploy/certs/ so
# deployment doesn't need to fetch it separately. To refresh (AWS rotates CAs every
# few years):
#   curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
#        -o deploy/certs/aws-rds-global-bundle.pem
AWS_RDS_CA = os.environ.get(
    "AWS_RDS_CA",
    str(BASE_DIR / "deploy" / "certs" / "aws-rds-global-bundle.pem"),
)


def _aurora_db_options(connect_timeout=5):
    """Build the OPTIONS dict for an Aurora database entry.

    Used in settings_local_multitenant.py when defining production DATABASES entries.
    Returns connect_timeout + verify-full TLS using the vendored AWS RDS CA.
    """
    return {
        "connect_timeout": connect_timeout,
        "sslmode":         "verify-full",
        "sslrootcert":     AWS_RDS_CA,
    }


def _proxy_db_options(connect_timeout=5):
    """Build the OPTIONS dict for a database entry that connects through RDS Proxy.

    Unlike a direct Aurora connection, RDS Proxy presents an ACM certificate that
    chains to the public Amazon Trust Services / Starfield roots - NOT the Amazon
    RDS CA in AWS_RDS_CA. So verify-full must validate against the OS trust store,
    which contains those roots.

    We point sslrootcert at the OS bundle FILE, not the special value "system":
    with the psycopg binary wheel (bundled libpq + OpenSSL), "system" resolves to
    the wheel's compiled-in OpenSSL dir, NOT the distro's /etc/ssl/certs, so it
    fails with "certificate verify failed". An explicit path is honored regardless
    of impl. Override PROXY_CA_BUNDLE if the OS bundle lives elsewhere (RHEL:
    /etc/pki/tls/certs/ca-bundle.crt). Used in settings_local_multitenant.py for
    DATABASES entries whose HOST is a *.proxy-*.rds.amazonaws.com endpoint.
    """
    return {
        "connect_timeout": connect_timeout,
        "sslmode":         "verify-full",
        "sslrootcert":     os.environ.get(
            "PROXY_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt"),
    }

# Platform base domains, for reference / future base-scoped rules. NOT read on the request
# path — tenant resolution is by full Host. Reserved-host enforcement lives entirely in
# tenants.ReservedHostRule (seeded in migration 0002): the service subdomains
# (www/api/admin/...) are reserved GLOBALLY, and the apexes below as EXACT rules. Kept here
# so the set of bases has one documented home.
TENANT_BASE_DOMAINS = ("routegenie.com", "isi-technology.com")


# ---------------------------------------------------------------------------
# Middleware — DELTA over the standalone base (imported above), NOT a rewrite. We inherit the
# base list and insert ONLY the tenant-specific middlewares at named anchors, so any middleware
# later added to the base flows into MT automatically (no parallel list to keep in sync). SYNC —
# the shard schema must be set on the same connection/thread the ORM later uses. Relative order
# (enforced by the tenants.E004 check):
# 1. ShardAwareTenantMiddleware    -> resolves tenant (+shard) from Host, sets
#                                     request.tenant and the schema on `default`.
# 2. TenantShardRoutingMiddleware  -> sets current_db (router -> shard DB) and the tenant
#                                     schema on the SHARD connection, and resets on the way out.
# DiagnosticsHeadersMiddleware (host/pid/alias response headers) is OPTIONAL and NOT inserted
# here — add it in settings_local_multitenant.py right after TenantShardRoutingMiddleware (current_db still
# live) if you want it.
# CORS is OUTERMOST on purpose (DB-independent): it answers preflight OPTIONS before tenant
# resolution and adds CORS headers even to the tenant middleware's short-circuited responses
# (e.g. the deactivated-tenant 403) so the browser sees them — hence the inserts go AFTER it.
# A base anchor that ever disappears makes its inserts silently vanish here — but the
# tenants.E004 check (deploy-time) then fails because the tenant middlewares are missing,
# so the ordering invariant stays guarded.
# ---------------------------------------------------------------------------
# Each base "anchor" middleware -> the tenant middlewares spliced in IMMEDIATELY after it
# (steps 1-2 above; SchemaBoundSession after Auth as it reads request.user). _MT_INSERTS is
# underscore-prefixed: Django ignores it as a setting, and `import *` does not re-export it.
_MT_INSERTS = {
    "corsheaders.middleware.CorsMiddleware": (
        "tenants.middleware.ShardAwareTenantMiddleware",
        "tenants.middleware.TenantShardRoutingMiddleware",
    ),
    "django.contrib.auth.middleware.AuthenticationMiddleware": (
        "users.middleware.SchemaBoundSessionMiddleware",
    ),
}
# Rebuild in ONE pass over the base list: keep each middleware, then append any inserts
# anchored to it. Base additions flow through untouched, and no temp index/variable is
# left behind (comprehension scopes don't leak in Python 3).
MIDDLEWARE = [mw for base_mw in MIDDLEWARE
              for mw in (base_mw, *_MT_INSERTS.get(base_mw, ()))]


# ---------------------------------------------------------------------------
# Auth: schema-bound JWT (augment the base REST_FRAMEWORK). Rejects a token whose `schema`
# claim != the request's tenant, so a token cannot be reused across tenants (CRITICAL #2).
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_AUTHENTICATION_CLASSES": (
    "users.authentication.SchemaBoundJWTAuthentication",
)}


# ---------------------------------------------------------------------------
# Caches — BUILT FROM SCRATCH, deliberately not merged into the base dict, for the same
# reason DATABASES is rebuilt above: the base is the STANDALONE host project's config, and
# multi-tenant must not inherit whatever instance or options that project happens to name.
# Every alias this mode runs on is declared here, so one read of this block is the whole
# picture instead of a diff against another file.
#
# `default` is TENANT-SCOPED and the only one that is. Every key the Django cache API touches
# becomes `tenant:<schema>:<prefix>:<version>:<key>` (commons.platform.cache_keys). The
# leading literal is the point — `tenant:<schema>:*` is then ONE glob covering this tenant's
# whole Redis footprint, including manual redis-py keys written through
# commons.platform.redis_client. That is what per-tenant flush on tenant deletion and
# per-tenant memory accounting need.
#
# NOT django_tenants.cache.make_key: it emits `<schema>:<prefix>:<version>:<key>` — schema
# first, but no shared literal to glob on, and nothing the WebSocket service could agree
# with. Recorded as a deliberate non-use in deploy/UPSTREAM_FORK.md.
#
# Switching scoping on is a COLD CACHE for `default` — every existing key changes shape.
#
# The other three are unscoped, each for a reason of its own:
#   sessions        the backing store is SHARED. django.contrib.sessions is in SHARED_APPS
#                   only, so `django_session` lives in the public schema and nowhere else —
#                   one row per session, whatever host issued it. Scoping its cache would
#                   partition the cache differently from the table it caches, and
#                   cached_db.load() returns a cache hit WITHOUT consulting the DB while
#                   cached_db.delete() clears only the CURRENT schema's key — so invalidation
#                   would be narrower than the thing invalidated. The isolation it would buy
#                   is zero: cross-tenant reuse is rejected by
#                   users.middleware.SchemaBoundSessionMiddleware, and a per-schema key never
#                   prevented the load anyway (a miss falls through to the same shared table).
#   tenant_resolve  resolution runs BEFORE the schema is known (see its own block below).
#   beat_lock       set and checked in the public-context dispatcher (see its block below).
#
# LOCATION: `default` and `sessions` are two aliases on ONE instance — django_redis caches
# pools process-globally by LOCATION (django_redis/pool.py, ConnectionFactory._pools), so a
# shared URL means a shared pool and no extra connections. They are still SEPARATE KEYS in
# the local settings file: overriding only `default` there leaves sessions on localhost.
# tenant_resolve and beat_lock need different EVICTION POLICIES, which is why they are
# separate instances rather than more aliases.
# ---------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND":  "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",   # dev default; prod -> settings_local_multitenant.py
        "KEY_PREFIX": "app",
        "KEY_FUNCTION":         "commons.platform.cache_keys.make_key",
        "REVERSE_KEY_FUNCTION": "commons.platform.cache_keys.reverse_key",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # FAIL-LOUD, unlike the standalone base (which fails open). A silently-swallowed
            # connection error is how a misconfigured LOCATION stays invisible: the cache just
            # "misses" forever and the app keeps serving from the DB. Under multi-tenant that
            # is the difference between a degraded cache and a cache that is quietly pointing
            # at the wrong instance, so the trade is made the other way here.
            #
            # The cost is real: with this False, a Redis outage becomes 5xx on any path that
            # touches this alias, instead of a slow-but-working request. Blast radius today is
            # small (nothing in app code reads `default` yet — the consumers are
            # tenant_resolve and beat_lock, and sessions have their own alias below), but it
            # grows the moment business code starts caching. If that trade stops being worth
            # it, the middle ground is IGNORE_EXCEPTIONS=True plus the global
            # DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS=True, which keeps fail-open but stops the
            # swallow from being silent.
            "IGNORE_EXCEPTIONS": False,
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
        },
    },
    "sessions": {
        "BACKEND":  "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",   # same instance as `default`, different prefix
        "KEY_PREFIX": "sess",
        # NO KEY_FUNCTION — see the note above.
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # Fail-OPEN here, deliberately differing from `default`: cached_db.load()/save()
            # already swallow cache errors themselves and fall back to the DB, but
            # cached_db.delete() does NOT — with this False, logging out during a Redis
            # outage would raise. Sessions stay available; the DB is the source of truth.
            "IGNORE_EXCEPTIONS": True,
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
        },
    },

    # ---------------------------------------------------------------------------
    # Tenant-resolution cache (host -> Tenant+shard). Read by ShardAwareTenantMiddleware on
    # EVERY request BEFORE the tenant is known, to remove the per-request Domain lookup from
    # the shared `default` DB.
    #
    # MUST be a SEPARATE Redis INSTANCE (not just another alias on `default`), maxmemory-policy
    # = volatile-ttl. What that buys differs BY STAGE, so both are spelled out:
    #   WARM off — every entry carries a TTL (positive 3600s, miss 60s, hold 5s). Under memory
    #     pressure Redis evicts the nearest-to-expiry (the short-TTL misses) first, protecting
    #     positives, and never has to refuse a write.
    #   WARM on  — ACTIVE positives are written with NO expiry (TENANT_RESOLVE
    #     ["WARM_TTL_BY_STATUS"]), and the gate's `treg:hosts` SET has none either. volatile-ttl
    #     never evicts a key that has no TTL — which is precisely what we want here (the registry
    #     survives memory pressure; only the disposable TTL-bearing entries absorb it). The
    #     trade-off: the "never refuses a write" property is GONE — once maxmemory is reached and
    #     only no-TTL keys remain, writes fail with OOM. So SIZE maxmemory for the domain count
    #     and alert on used_memory: ~250 B per entry (a snapshot pickles to ~134 B, plus key and
    #     overhead), one entry per Domain plus one per Tenant → ~50 MiB at 100k domains. The set
    #     of no-TTL keys never shrinks on its own; reconcile's orphan-sweep is what bounds it.
    # Resolution runs in the PUBLIC context, so keys carry only a static
    # KEY_PREFIX ("tres"), never a per-tenant KEY_FUNCTION. IGNORE_EXCEPTIONS + short timeouts
    # => a slow/down instance degrades to a `default` DB lookup (fail-open). Point LOCATION at
    # the dedicated instance in settings_local_multitenant.py.
    # ---------------------------------------------------------------------------
    "tenant_resolve": {
        "BACKEND":  "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/2",   # dev default; prod -> settings_local_multitenant.py
        "KEY_PREFIX": "tres",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,             # Redis down/slow => miss => DB (fail-open)
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
        },
    },

    # Cross-tenant coordination cache for the fanout overlap-lock
    # (tenants.celery.dispatch._acquire_lock). Points at the BROKER Redis, which is NOEVICTION
    # — a lock here is never dropped under memory pressure (the app `default` cache is
    # allkeys-lru, where an evicted lock mid-wave would let fan-outs overlap). Tenant-agnostic
    # by construction: a STATIC KEY_PREFIX, never a per-tenant KEY_FUNCTION (the lock is
    # set+checked in the public-context dispatcher). IGNORE_EXCEPTIONS is OFF here (unlike the
    # fail-open tenant_resolve cache): if the lock Redis is DOWN, cache.add RAISES and
    # fanout_dispatch fails LOUDLY (surfaces the outage) rather than silently skipping the tick.
    # This is the same instance RedBeat takes its scheduler lock on.
    "beat_lock": {
        "BACKEND":  "django_redis.cache.RedisCache",
        "LOCATION": CELERY_BROKER_URL,             # broker Redis (noeviction); rediss:// => SSL via redis-py
        "KEY_PREFIX": "beatlock",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # NO IGNORE_EXCEPTIONS here: this is a coordination lock, not a disposable cache — if
            # the lock Redis is DOWN, cache.add RAISES and fanout_dispatch fails LOUDLY (surfaces the
            # outage) instead of silently skipping ticks.
            "IGNORE_EXCEPTIONS": False,
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
        },
    },
}

# Sessions move OFF `default` under multi-tenant — see the `sessions` note above. The base
# keeps them on `default`, which is correct there (no KEY_FUNCTION, nothing to separate).
SESSION_CACHE_ALIAS = "sessions"


# ---------------------------------------------------------------------------
# Resolver knobs — two namespaced dicts over the in-code DEFAULTS, merged PER KEY and read
# live. Same convention as TENANT_BEAT above: SHIP THEM EMPTY and add only what differs from
# the default, here or in settings_local_multitenant.py.
#
# Both used to restate the defaults in full. All twelve values were byte-identical to them, so
# the blocks changed nothing — while creating a second copy that silently WINS over the code:
# lower a default in config.py and the stale value here keeps overriding it, with no error and
# no failing test. Restating a default is therefore not documentation, it is a trap.
#
# Names only below, so you know what exists without opening the file; the values, units and
# the reasoning for each live beside the defaults:
#
#   TENANT_RESOLVE   — resolution-cache tuning
#       POSITIVE_CACHE_SECONDS  MISS_CACHE_SECONDS  HOLD_SECONDS
#       WARM_TTL_BY_STATUS  FILLCAP_PER_SEC  FILLCAP_LOCAL_PER_SEC
#   TENANT_REGISTRY  — anti-DoS host gate, two-stage rollout, both flags default OFF
#       WARM_ENABLED  GATE_ENABLED  HOSTS_ARM_SECONDS
#       WARM_LOCK_SECONDS  WARM_PENDING_SECONDS
#
# There is deliberately no RECONCILE_SECONDS knob. The daily safety reconcile is the
# `resolve-gate-reconcile` beat entry below — crontab(minute=0, hour=4) — and that entry is
# the single place its cadence is stated. A seconds knob used to sit in REGISTRY_DEFAULTS
# next to it, read by nothing: turning it down would have changed no behaviour at all, while
# looking exactly like the control for this schedule.
#
# GATE_ENABLED requires WARM_ENABLED: the tenants.E001 check reads these RAW dicts (not the
# _Namespace view) precisely so the runtime fail-safe cannot hide that misconfiguration.
# Leaving them empty keeps that working — an absent key reads as False, which is the safe
# state, and a hand-set GATE without WARM still trips the check.
#
# Defaults with values and per-key reasoning: tenants/resolver/config.py
# (RESOLVE_DEFAULTS / REGISTRY_DEFAULTS).
# ---------------------------------------------------------------------------
TENANT_RESOLVE = {}
TENANT_REGISTRY = {}


# ---------------------------------------------------------------------------
# Celery — queues, fanout knobs, schedule, and HA beat (RedBeat). Standalone uses Celery's
# single default queue + stock beat; all of this is MT-only. Routing lives at the task
# level via @shared_task(queue=task_queue("...")); task_queue() returns None in standalone.
# ---------------------------------------------------------------------------
#   fast    - short, latency-sensitive tasks (default)
#   slow    - long-running tasks (provisioning, migrations, bulk jobs)
#   service - maintenance / housekeeping (reconcile, provisioning)
#   fanout  - fanout_dispatch / sub_dispatch (schedule fan-out waves)
CELERY_TASK_QUEUES = [Queue("fast"), Queue("slow"), Queue("service"), Queue("fanout")]
CELERY_TASK_DEFAULT_QUEUE = "fast"

# Runtime OVERRIDES for the fanout dispatcher. Unspecified keys fall back to the in-code
# defaults (commons.platform.beat.BEAT_DEFAULTS): TZ_GRACE_SECONDS=300, BATCH_SIZE=100,
# LOCK_SECONDS=60. Empty {} = use all defaults; set a key here (or in settings_local_multitenant.py) to
# override. NOTE: the calendar beat tick is NOT a runtime knob — override it PER schedule entry
# via scoped_schedule(..., fanout_period=<seconds>), not here (it is baked in at settings load).
TENANT_BEAT = {}

# schema -> Tenant(+shard) lookup cache, per worker process. Read solely by
# tenants.celery.task.TenantTask.get_tenant_for_schema. 0 = no cache (always fresh).
CELERY_TASK_TENANT_CACHE_SECONDS = int(os.environ.get("CELERY_TASK_TENANT_CACHE_SECONDS", "30"))

# The beat schedule is a settings dict (django-celery-beat is gone). AUGMENT the base
# schedule (host/business entries) — never replace it — and route EVERY entry through
# scoped_schedule so the model is uniform:
#   scoped_schedule(entry, scope="public")   -> identity (runs once, e.g. in the public schema)
#   scoped_schedule(entry, scope="tenants")  -> fanned out per tenant
#       crontab   => per-tenant LOCAL time;  number/timedelta => interval, all tenants.
CELERY_BEAT_SCHEDULE = {
    **CELERY_BEAT_SCHEDULE,   # base/host business schedule (may be empty)

    # resolve-gate reconcile — daily safety net; scope="public" => runs ONCE in the public
    # schema (no fanout). See deploy/resolve_gate_design.md.
    "resolve-gate-reconcile": scoped_schedule(
        {
            "task": "tenants.tasks.reconcile_host_registry_task",
            "schedule": crontab(minute=0, hour=4),
        },
        scope="public",
    ),
}

# HA beat: RedBeat elects ONE active scheduler among multiple beat replicas via a Redis
# lock — run 2-3 replicas under a supervisor; a standby takes over on failure (a deposed
# node exits and is restarted as standby). RedBeat reads CELERY_BEAT_SCHEDULE + these
# CELERY_REDBEAT_* keys from app.conf.
CELERY_BEAT_SCHEDULER = "redbeat.RedBeatScheduler"
CELERY_REDBEAT_KEY_PREFIX = "redbeat:"
CELERY_REDBEAT_LOCK_TIMEOUT = 90   # seconds; >= beat loop interval — standby takes over after this
# Set explicitly (RedBeat 2.5+ drops the broker_url fallback). Defaults to the broker
# (noeviction). Override in settings_local_multitenant.py to a dedicated NOEVICTION Redis — NEVER the
# eviction `tenant_resolve` cache (its keys can be evicted). celery.py also sets
# app.conf.redbeat_redis_url directly so RedBeat's `in`-based is_key_in_conf sees it.
CELERY_REDBEAT_REDIS_URL = os.environ.get("REDBEAT_REDIS_URL", CELERY_BROKER_URL)
# Mirrors CELERY_BROKER_USE_SSL. Without it RedBeat inherits the value from the broker through
# an either_or fallback that upstream does not document and has started removing (CHANGES.txt
# 2.4.2 deprecates the broker_url / broker_transport_options fallbacks for 2.5.0).
#
# NOT load-bearing today, and the earlier claim that losing the fallback would break beat's TLS
# was wrong: get_redis()'s `rediss://` branch starts from its own {'ssl_cert_reqs':
# ssl.CERT_REQUIRED} and merely UPDATES it from this value, so verification stays on either
# way. It IS load-bearing for the redis-sentinel branch, which has no such default — we do not
# use sentinel, but stating the posture here beats inheriting it silently.
CELERY_REDBEAT_REDIS_USE_SSL = {"ssl_cert_reqs": "required"}
