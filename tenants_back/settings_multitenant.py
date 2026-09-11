"""Multi-tenant settings layer.

Reassembled/overridden ON TOP of the standalone base in settings.py. Loaded ONLY when
USE_MULTITENANT, from the bottom of settings.py, BEFORE settings_local (so production
overrides still win over both). This file holds ALL the multi-tenant-specific config so
settings.py stays a clean standalone/shared base.

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
# brings in every base setting this file augments (CACHES, MIDDLEWARE, DATABASES,
# REST_FRAMEWORK, CELERY_BROKER_URL, CELERY_BEAT_SCHEDULE, ...); the second import is only
# for the underscore-prefixed app blocks, which `import *` deliberately skips.
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

# django-tenants backend (adds the schema_name connection attribute; wraps PostGIS via
# ORIGINAL_BACKEND). DERIVES from the base DATABASES["default"] (plain PostGIS) WITHOUT
# mutating it: rebuild the nested "default" so the base module's dict stays pristine.
DATABASES = {**DATABASES,
             "default": {**DATABASES["default"],
                         "ENGINE": "django_tenants.postgresql_backend"}}
ORIGINAL_BACKEND = "django.contrib.gis.db.backends.postgis"

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
# here — add it in settings_local.py right after TenantShardRoutingMiddleware (current_db still
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
# the dedicated instance in settings_local.py.
# ---------------------------------------------------------------------------
CACHES = {**CACHES, "tenant_resolve": {       # merge-rebind (shallow): adds top-level keys; do NOT mutate nested base sub-dicts
    "BACKEND":  "django_redis.cache.RedisCache",
    "LOCATION": "redis://127.0.0.1:6379/2",   # dev default; prod -> settings_local.py
    "KEY_PREFIX": "tres",
    "OPTIONS": {
        "CLIENT_CLASS": "django_redis.client.DefaultClient",
        "IGNORE_EXCEPTIONS": True,             # Redis down/slow => miss => DB (fail-open)
        "SOCKET_CONNECT_TIMEOUT": 1,
        "SOCKET_TIMEOUT": 1,
    },
}}

# Cross-tenant coordination cache for the fanout overlap-lock
# (tenants.celery.dispatch._acquire_lock). Points at the BROKER Redis, which is NOEVICTION
# — a lock here is never dropped under memory pressure (the app `default` cache is
# allkeys-lru, where an evicted lock mid-wave would let fan-outs overlap). Tenant-agnostic
# by construction: a STATIC KEY_PREFIX, never a per-tenant KEY_FUNCTION (the lock is
# set+checked in the public-context dispatcher). IGNORE_EXCEPTIONS is OFF here (unlike the
# fail-open tenant_resolve cache): if the lock Redis is DOWN, cache.add RAISES and
# fanout_dispatch fails LOUDLY (surfaces the outage) rather than silently skipping the tick.
# This is the same instance RedBeat takes its scheduler lock on.
CACHES = {**CACHES, "beat_lock": {            # merge-rebind: do NOT mutate the base CACHES obj
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
}}


# ---------------------------------------------------------------------------
# Resolver settings — two namespaced dicts over in-code DEFAULTS (tenants/resolver/config.py).
# Override per key here (or in settings_local.py); unspecified keys fall back to DEFAULTS.
# Full design: deploy/resolve_gate_design.md.
# ---------------------------------------------------------------------------
# TENANT_RESOLVE — resolution-cache tuning. POSITIVE/MISS_CACHE_SECONDS = positive/negative
# TTLs; HOLD_SECONDS = invalidation tombstone (closes the read-then-write race);
# WARM_TTL_BY_STATUS = per-status positive TTL under WARM (None => no expiry);
# FILLCAP_* = DB-resolve rate limit on the flag-absent branch.
TENANT_RESOLVE = {
    "POSITIVE_CACHE_SECONDS": 3600,
    "MISS_CACHE_SECONDS": 60,
    "HOLD_SECONDS": 5,
    "WARM_TTL_BY_STATUS": {
        "active": None, "deactivated": 3600, "failed": 1800, "new": 120, "pending": 120,
    },
    "FILLCAP_PER_SEC": 20,
    "FILLCAP_LOCAL_PER_SEC": 5,
}

# TENANT_REGISTRY — anti-DoS host gate, two-stage rollout. Defaults OFF. WARM_ENABLED
# (write side: maintain the `treg:hosts` SET + reconcile); GATE_ENABLED (read side: reject
# unknown hosts on a cache miss without a DB hit; requires WARM — the tenants.E001 check
# flags GATE-without-WARM). HOSTS_ARM_SECONDS/RECONCILE_SECONDS/WARM_LOCK_SECONDS/
# WARM_PENDING_SECONDS tune the dead-man switch, daily reconcile, writer lock, and enqueue
# coalescing respectively.
TENANT_REGISTRY = {
    "WARM_ENABLED": False,
    "GATE_ENABLED": False,
    "HOSTS_ARM_SECONDS": 300,
    "RECONCILE_SECONDS": 86400,
    "WARM_LOCK_SECONDS": 120,
    "WARM_PENDING_SECONDS": 10,
}


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
# LOCK_SECONDS=60. Empty {} = use all defaults; set a key here (or in settings_local.py) to
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
# (noeviction). Override in settings_local.py to a dedicated NOEVICTION Redis — NEVER the
# eviction `tenant_resolve` cache (its keys can be evicted). celery.py also sets
# app.conf.redbeat_redis_url directly so RedBeat's `in`-based is_key_in_conf sees it.
CELERY_REDBEAT_REDIS_URL = os.environ.get("REDBEAT_REDIS_URL", CELERY_BROKER_URL)
