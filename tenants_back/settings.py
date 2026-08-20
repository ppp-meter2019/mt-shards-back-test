"""Settings for tenants_back - multi-tenant Django/DRF on multi-DB Aurora."""

import os
from datetime import timedelta
from pathlib import Path

from kombu import Queue

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-3!1c9_e7icl-bz4bf$_1c5k_^vo43bm1ia66uce$zeyf^6(vvn",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()
]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

# AWS RDS Certificate Authority bundle, used by psycopg's sslrootcert when
# DB_SSL=1 to verify Aurora's TLS certificate. The file is vendored in the
# repo at deploy/certs/ so deployment doesn't need to fetch it separately.
# To refresh (AWS rotates CAs every few years):
#   curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
#        -o deploy/certs/aws-rds-global-bundle.pem
AWS_RDS_CA = os.environ.get(
    "AWS_RDS_CA",
    str(BASE_DIR / "deploy" / "certs" / "aws-rds-global-bundle.pem"),
)

# TLS / reverse-proxy settings (SECURE_PROXY_SSL_HEADER, USE_X_FORWARDED_HOST,
# SESSION_COOKIE_SECURE, CSRF_COOKIE_SECURE) are NOT set here - they would
# break local dev where Django runs on plain http://localhost. Production
# values live in settings_local.py (see settings_local.py.example).


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
# IMPORTANT: 'tenants' MUST come BEFORE 'django_tenants' so our management
# commands (notably migrate_schemas) override the upstream versions.
SHARED_APPS = [
    "tenants",
    "django_tenants",

    "django.contrib.contenttypes",
    "django.contrib.auth",
    # PostGIS: GIS field types + GIS ORM. No tables of its own here, but the
    # backend (ORIGINAL_BACKEND below) needs the extension to be installed in
    # every database. See README "Bootstrap" for `CREATE EXTENSION postgis`.
    "django.contrib.gis",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",

    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",

    # Celery beat schedules live per-schema (public + each tenant), so this is
    # in BOTH SHARED_APPS and TENANT_APPS. See tenants.celery.db_scheduler.
    "django_celery_beat",

    "users",
]

TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",

    "django_celery_beat",

    "users",

    "customers",
    "drivers",
    "cars",
    "products",
    "orders",
    "routes",
]

# django-tenants requires INSTALLED_APPS to be the de-duplicated union.
INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]


# ---------------------------------------------------------------------------
# django-tenants
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
# We list our router FIRST (it fully overrides db_for_read/write + allow_migrate,
# so it always decides); the upstream name is a no-op fallback that only
# satisfies that check.
DATABASE_ROUTERS = [
    "tenants.routers.TenantDatabaseRouter",
    "django_tenants.routers.TenantSyncRouter",
]

# PostGIS backend (required for GIS models in tenant apps such as orders).
ORIGINAL_BACKEND = "django.contrib.gis.db.backends.postgis"

# Platform base domains, for reference / future base-scoped rules. NOT read on the
# request path — tenant resolution is by full Host. Reserved-host enforcement lives
# entirely in tenants.ReservedHostRule (seeded in migration 0004): the service
# subdomains (www/api/admin/...) are reserved GLOBALLY, and the apexes below are
# reserved as EXACT rules. Kept here so the set of bases has one documented home.
TENANT_BASE_DOMAINS = ("routegenie.com", "isi-technology.com")


# ---------------------------------------------------------------------------
# Worker model: this project runs a SYNC server — Gunicorn `sync` (prefork)
# workers over WSGI. This is the simplest, most robust model for our sync code
# + django-tenants (one request per process; connections are per-process; the
# tenant schema is set/reset per request). Concurrency = number of worker
# processes. CPU-heavy work belongs in Celery (phase 2), not the web tier.
#
# The async path (UvicornWorker / ASGI) is intentionally NOT used: with our
# required sync tenant/shard middleware it gives no concurrency win and can even
# regress vs prefork. See README "Architecture trade-offs" before switching.
#
# Middleware order matters (both are SYNC — the shard schema must be set on the
# same connection/thread the ORM later uses):
# 1. ShardAwareTenantMiddleware    -> resolves tenant (+shard) from Host, sets
#                                     request.tenant and the schema on `default`.
# 2. TenantShardRoutingMiddleware  -> sets current_db (router -> shard DB) and the
#                                     tenant schema on the SHARD connection, and
#                                     resets both on the way out.
# 3. DiagnosticsHeadersMiddleware  -> stamps response with host/pid/alias (MVP
#                                     demo). Inside (2) so current_db is still live.
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    # CORS is OUTERMOST on purpose. It is DB-independent (only reads the Origin
    # header and adds response headers), so wrapping everything is safe and does
    # NOT violate the "tenant/shard set before any DB access" rule below — the
    # tenant middlewares still precede every DB-touching middleware.
    # Being outermost lets CorsMiddleware (a) answer preflight OPTIONS before
    # tenant resolution, and (b) add CORS headers on the way out even to the
    # tenant middleware's SHORT-CIRCUITED responses (e.g. the deactivated-tenant
    # 403) — without this the browser blocks that cross-origin 403 ("Failed to
    # fetch") and never sees the message.
    "corsheaders.middleware.CorsMiddleware",
    "tenants.middleware.ShardAwareTenantMiddleware",
    "tenants.middleware.TenantShardRoutingMiddleware",
    "tenants.middleware_diagnostics.DiagnosticsHeadersMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "users.middleware.SchemaBoundSessionMiddleware",  # tenant-bind session auth (after Auth)
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ---------------------------------------------------------------------------
# Templates / WSGI (sync prefork — see "Worker model" block above).
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "tenants_back.wsgi.application"
ASGI_APPLICATION = None


# ---------------------------------------------------------------------------
# Databases.
#
# Only the `default` alias is defined here, with dev defaults pointing at a
# local Postgres. Production overrides this entry and adds the `tenant_*`
# shards in settings_local.py - use the _aurora_db_options() helper there to
# build per-cluster OPTIONS (connect_timeout + verify-full TLS against AWS
# RDS CA).
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE":             "django_tenants.postgresql_backend",
        "NAME":               "tenants_back",
        "USER":               "postgres",
        "PASSWORD":           "postgres",
        "HOST":               "127.0.0.1",
        "PORT":               "5432",
        "CONN_MAX_AGE":       60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS":            {"connect_timeout": 5},
    },
}


def _aurora_db_options(connect_timeout=5):
    """Build the OPTIONS dict for an Aurora database entry.

    Used in settings_local.py when defining production DATABASES entries.
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
    /etc/pki/tls/certs/ca-bundle.crt). Used in settings_local.py for DATABASES
    entries whose HOST is a *.proxy-*.rds.amazonaws.com endpoint.
    """
    return {
        "connect_timeout": connect_timeout,
        "sslmode":         "verify-full",
        "sslrootcert":     os.environ.get(
            "PROXY_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt"),
    }


# ---------------------------------------------------------------------------
# Auth + DRF + JWT
# (Frontend contract is preserved: /api/auth/login/ returns access/refresh/role/schema.)
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # Schema-bound: rejects a token whose `schema` claim != the request's
        # tenant, so a token cannot be reused across tenants (CRITICAL #2).
        "users.authentication.SchemaBoundJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME":  timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}


# ---------------------------------------------------------------------------
# Cache: a single Redis for the app cache + Django sessions. In production this
# maps to one ElastiCache cluster (maxmemory-policy=allkeys-lru is fine — it's
# a disposable cache; sessions also live in the DB via cached_db, see below).
# ---------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND":  "django_redis.cache.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/1",
        "KEY_PREFIX": "app",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,
            "SOCKET_CONNECT_TIMEOUT": 1,
            "SOCKET_TIMEOUT": 1,
        },
    },
}

# Dedicated cache alias for Celery-beat coordination keys (beat:last_change:<schema>).
# These are CROSS-TENANT markers: written in a tenant's context but read by beat
# outside any tenant context, so they must NOT carry the per-tenant key prefix used
# for app data. Same Redis by default; override BEAT_REDIS_URL / LOCATION if needed.
#
# OPTIONS is a COPY of default's, NOT a shared reference: when the app-data caches
# gain a tenant KEY_FUNCTION, add it to CACHES["default"]["OPTIONS"] ONLY — never
# here. A tenant prefix on this alias would split beat's writer (tenant context)
# and reader (public context) onto different keys and silently break change-detection.
CACHES["beat"] = {
    "BACKEND":    CACHES["default"]["BACKEND"],
    "LOCATION":   os.environ.get("BEAT_REDIS_URL", CACHES["default"]["LOCATION"]),
    "KEY_PREFIX": "beat",
    "OPTIONS":    dict(CACHES["default"].get("OPTIONS", {})),
}

# ---------------------------------------------------------------------------
# Tenant-resolution cache (host -> Tenant+shard). Read by ShardAwareTenantMiddleware
# on EVERY request BEFORE the tenant is known, to remove the per-request Domain
# lookup from the shared `default` DB — so a worker serving cache-hit traffic stops
# touching `default` and its connection idles out (fewer connections to `default`).
#
# This MUST be a SEPARATE Redis INSTANCE (not just another alias on `default`),
# because it needs a different maxmemory-policy than the app/session cache.
#
#   *** Required config on this instance (ElastiCache parameter group): ***
#     maxmemory-policy = volatile-ttl
#       Every entry here is written WITH a TTL (positive AND negative). Under memory
#       pressure Redis then evicts the NEAREST-to-expiry first — i.e. the short-TTL
#       negative (miss) entries — protecting the long-TTL positives, and KEEPS
#       accepting writes (unlike noeviction, which would start failing them).
#       NEVER write a key here without a TTL (no cache.set(..., timeout=None)):
#       a persistent key is not an eviction candidate and breaks this guarantee.
#     A dedicated instance also keeps this namespace tenant-agnostic: resolution
#     runs in the PUBLIC context (tenant not resolved yet), so keys must carry only
#     a static KEY_PREFIX ("tres") and NEVER a per-tenant KEY_FUNCTION — same as the
#     `beat` alias (which likewise uses a static prefix, no per-tenant key function).
#
# Failure mode: IGNORE_EXCEPTIONS + short timeouts => a slow/down instance degrades
# to a `default` DB lookup (fail-open) — today's behavior. Correctness never depends
# on this cache being up.
#
# Production: point LOCATION at the dedicated instance in settings_local.py.
# ---------------------------------------------------------------------------
CACHES["tenant_resolve"] = {
    "BACKEND":  "django_redis.cache.RedisCache",
    "LOCATION": "redis://127.0.0.1:6379/2",   # dev default; prod -> settings_local.py
    "KEY_PREFIX": "tres",
    "OPTIONS": {
        "CLIENT_CLASS": "django_redis.client.DefaultClient",
        "IGNORE_EXCEPTIONS": True,             # Redis down/slow => miss => DB (fail-open)
        "SOCKET_CONNECT_TIMEOUT": 1,
        "SOCKET_TIMEOUT": 1,
    },
}

# ---------------------------------------------------------------------------
# Resolver settings — two namespaced dicts over in-code DEFAULTS (tenants/resolver/config.py).
# Override per key here (or in settings_local.py); unspecified keys fall back to DEFAULTS.
# ---------------------------------------------------------------------------
# TENANT_RESOLVE — resolution-cache tuning (always relevant).
#   POSITIVE_CACHE_SECONDS: positive (success) TTL. Long backstop; correctness is kept by
#     explicit invalidation (Tenant/Domain signals). Only self-heals entries an invalidation
#     missed (e.g. a QuerySet.update()/migration status/shard change — no post_save). 0
#     disables positive caching. Do NOT make it unbounded.
#   MISS_CACHE_SECONDS: negative (miss) TTL. Short — bounds memory a flood of unknown hosts
#     can occupy. A new domain is made resolvable immediately by invalidating its negative on
#     creation, so this is only a safety bound. 0 disables negative caching.
#   HOLD_SECONDS: invalidation writes a short "hold" (tombstone) instead of deleting; while it
#     lives, resolve goes DB-direct and an nx populate cannot overwrite it (closes the
#     read-then-write race). 0 => plain delete (race open). Shortest-TTL key → volatile-ttl
#     evicts it first.
#   WARM_TTL_BY_STATUS: positive TTL by tenant status, used ONLY under WARM. None => no expiry
#     (kept warm by reconcile + orphan-sweep). Statuses absent here fall back to
#     POSITIVE_CACHE_SECONDS.
#   FILLCAP_PER_SEC / FILLCAP_LOCAL_PER_SEC: fill_cap rate-limits DB resolves ONLY on the
#     flag-absent branch (cold / flush / warm-in-progress). Global (Redis fixed-window) with a
#     per-pod bucket fallback. Member cold-fills and non-member rejects do NOT consume it.
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

# TENANT_REGISTRY — anti-DoS host gate, two-stage rollout. Defaults OFF (byte-for-byte
# today's behavior); enable per environment in settings_local.py. Full design:
# deploy/resolve_gate_design.md.
#   WARM_ENABLED (Stage 1, write side): maintain the `tres:hosts` SET (SADD/SREM + dead-man
#     switch), run reconcile, write positives with WARM_TTL_BY_STATUS. No read impact.
#   GATE_ENABLED (Stage 2, read side): on a positive-cache MISS, consult `tres:hosts` and
#     reject unknown (non-member) hosts WITHOUT a DB hit. Requires WARM (fail-safe: GATE
#     without WARM is treated as OFF; the tenants.E001 check flags the misconfig).
#   HOSTS_ARM_SECONDS: dead-man TTL armed on domain add/delete/rename — if the follow-up
#     reconcile never runs, the key expires and the gate fails open.
#   RECONCILE_SECONDS: daily safety reconcile (catches signal-bypassing drift). NB: consumed
#     by the ops beat schedule, not read by code.
#   WARM_LOCK_SECONDS: `tres:warming` single-writer lock TTL. INVARIANT: must comfortably
#     exceed the WORST-CASE reconcile (full DB scan × up to 3 dirty-recheck rebuilds); if it
#     expires mid-reconcile, a second writer can overlap. ~sub-second at ~700 tenants → 120s
#     is ample; raise it if domain count / DB latency grows.
#   WARM_PENDING_SECONDS: reconcile-trigger enqueue-coalescing window (`tres:warm_pending`,
#     self-expiring NX). Also the worst-case re-enqueue delay if a broker publish was lost.
TENANT_REGISTRY = {
    "WARM_ENABLED": False,
    "GATE_ENABLED": False,
    "HOSTS_ARM_SECONDS": 300,
    "RECONCILE_SECONDS": 86400,
    "WARM_LOCK_SECONDS": 120,
    "WARM_PENDING_SECONDS": 10,
}

# API path prefixes — request-handling code that treats API traffic as stateless/JSON:
# the session guard (users.middleware) and error content negotiation (tenants.errors).
# Single source of truth so the two stay in sync.
API_PATH_PREFIXES = ("/api/v1/", "/open_api/api/v1/")

# Celery-beat change-detection: beat polls every max_interval; keep the per-tenant
# Redis change-markers alive for k×max_interval (k>=3) so a running beat always
# observes a marker before it expires, and deleted-tenant markers self-clean.
DJANGO_CELERY_BEAT_MAX_LOOP_INTERVAL = int(os.environ.get("BEAT_MAX_LOOP_INTERVAL", "5"))
BEAT_MARKER_TTL_SECONDS = DJANGO_CELERY_BEAT_MAX_LOOP_INTERVAL * 3

SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_CACHE_ALIAS = "default"


# ---------------------------------------------------------------------------
# i18n / static
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Where to send users after Django-admin login on each schema.
LOGIN_REDIRECT_URL = "/admin/"


# ---------------------------------------------------------------------------
# CORS - only relevant in split-origin dev. In production frontend and
# backend share an origin through ALB, so CORS is effectively unused.
# ---------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = os.environ.get("DJANGO_CORS_ALLOW_ALL", "1") == "1"
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
CORS_ALLOWED_ORIGIN_REGEXES = [
    r.strip() for r in os.environ.get("DJANGO_CORS_ALLOWED_ORIGIN_REGEXES", "").split(",") if r.strip()
]
CORS_ALLOW_CREDENTIALS = False


# ---------------------------------------------------------------------------
# Celery — shard+schema-aware tasks (tenants.celery). Namespace "CELERY":
# CELERY_FOO -> app.conf.foo. The broker is a SEPARATE Redis (noeviction),
# NOT the cache cluster, so queued tasks are never evicted under memory
# pressure. No result backend: provisioning state lives in Tenant.status.
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = os.environ.get(
    "CELERY_BROKER_URL",
    "rediss://master.test-multitenants.qmp0of.use2.cache.amazonaws.com:6379/0",
)
CELERY_BROKER_USE_SSL = {"ssl_cert_reqs": "required"}    # rediss:// → verify cert
CELERY_RESULT_BACKEND = None
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT  = ["json"]
CELERY_TIMEZONE        = TIME_ZONE
CELERY_TASK_ACKS_LATE  = True            # don't lose a task if a worker dies mid-run
CELERY_WORKER_PREFETCH_MULTIPLIER = 1    # fair dispatch for long tasks
# Three queues:
#   fast    - short, latency-sensitive tasks (default)
#   slow    - long-running tasks (provisioning, migrations, bulk jobs)
#   service - maintenance / housekeeping (reconcile, cleanup, beat-driven)
# Run workers per queue, e.g.  celery -A tenants_back worker -Q fast
#                              celery -A tenants_back worker -Q slow
#                              celery -A tenants_back worker -Q service
CELERY_TASK_QUEUES = (
    Queue("fast"),
    Queue("slow"),
    Queue("service"),
)
CELERY_TASK_DEFAULT_QUEUE = "fast"
CELERY_TASK_ROUTES = {
    "tenants.tasks.provision_tenant": {"queue": "service"},
    "tenants.tasks.drop_tenant_schema_task": {"queue": "service"},
}
# schema -> Tenant(+shard) lookup cache, per worker process. 0 = no cache
# (always fresh; safe if a tenant is ever moved to another shard). Override in
# settings_local.py (e.g. 10) when shard assignments are stable.
CELERY_TASK_TENANT_CACHE_SECONDS = int(
    os.environ.get("CELERY_TASK_TENANT_CACHE_SECONDS", "0")
)
# Tenant-aware DB-backed beat (fans periodic tasks out per tenant schema).
CELERY_BEAT_SCHEDULER = "tenants.celery.db_scheduler:TenantAwareDatabaseScheduler"


# ---------------------------------------------------------------------------
# S3 - offline GPS/coordinate storage (multi-tenant data lake). Part of moving
# coordinate storage off MongoDB (Jira IT-21249); objects are written under a
# per-tenant (numeric id), date-partitioned prefix so downstream analytics
# (Athena / Kinesis-Firehose, Jira IT-21374) can scan by tenant + day.
# Bucket + region are environment-specific -> override in settings_local.py.
# Credentials come from the instance / ECS-task IAM role (no keys in code).
# ---------------------------------------------------------------------------
AWS_S3_COORDINATES_BUCKET = os.environ.get("AWS_S3_COORDINATES_BUCKET", "")
AWS_S3_REGION = os.environ.get("AWS_S3_REGION", "") or None


# ---------------------------------------------------------------------------
# Local overrides last (production secrets, hostnames, etc.)
# ---------------------------------------------------------------------------
try:
    from .settings_local import *  # noqa: F403
except ImportError:
    print("Can't load local settings!")
