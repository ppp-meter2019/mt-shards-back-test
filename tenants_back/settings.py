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

# Resolution-cache TTLs. 0 disables that half (falls back to a DB lookup).
# positive: long backstop; correctness is kept by explicit invalidation (signals on
#           Tenant/Domain). The TTL only self-heals entries an invalidation missed —
#           e.g. a status/shard change via QuerySet.update()/migration, which does
#           NOT fire post_save. Do NOT make this unbounded.
# negative: short; bounds the memory a flood of unknown-host requests can occupy.
#           A just-created domain is made resolvable immediately by invalidating its
#           negative entry on Domain creation, so this TTL is only a safety bound.
TENANT_RESOLVE_CACHE_SECONDS      = 3600   # positive (success): 1 hour
TENANT_RESOLVE_MISS_CACHE_SECONDS = 60     # negative (miss): 60 s

# Invalidation writes a short-lived "hold" marker instead of deleting the key; while
# it lives, get_tenant resolves DB-direct and an nx=True populate CANNOT overwrite it —
# closing the read-then-write race (a slow resolver repopulating a stale snapshot just
# after invalidation). Self-heals on expiry. 0 => plain delete (no hold, race open).
# Keep it a few seconds (comfortably above a resolver's read->write latency). NOTE: it
# is the shortest-TTL key here, so volatile-ttl evicts it first under memory pressure.
TENANT_RESOLVE_HOLD_SECONDS = 5

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
