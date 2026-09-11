"""SHARED BASE settings for tenants_back — Django/DRF on PostGIS.

This file is complete on its own: it IS the standalone configuration. The multi-tenant
overlay (settings_multitenant.py) star-imports this module and augments it; `settings.py` is
a thin dispatcher that picks one of the two and then applies settings_local.py. See the
dispatcher's docstring for the layer order, and deploy/standalone_multitenant_design.md §3.1
for why the tiers are what they are.

Nothing here may import settings_multitenant: the dependency runs base <- overlay, one way.
That is the point of the split. The alternative — the overlay importing individual names back
out of this file — works only while every one of them happens to be defined above the
overlay's import line, i.e. it depends on statement order inside a module.
"""

import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-3!1c9_e7icl-bz4bf$_1c5k_^vo43bm1ia66uce$zeyf^6(vvn",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# Multi-tenant (django-tenants + shards) vs standalone (single default DB, no schemas).
# Resolved HERE, at import time, because it gates INSTALLED_APPS / DB backend+routers /
# middleware / admin below — so it CANNOT come from settings_local (imported last) or
# from the DB. Precedence, so the mode can be set WITHOUT an env var AND without editing
# this file:
#   1. env  USE_MULTITENANT=0/1                (CI / containers)
#   2. settings_mode.py -> USE_MULTITENANT     (gitignored local file; see the .example)
#   3. default False                           (STANDALONE is the default — the larger
#                                               host project's mode; multi-tenant is the
#                                               opt-in layer. THIS dev repo ships a local
#                                               settings_mode.py with True so day-to-day
#                                               work + tests stay multi-tenant.)
# Single source of truth (settings-load-safe; also used by commons.platform.beat.scoped_schedule
# which runs while the host builds CELERY_BEAT_SCHEDULE).
from commons.platform.mode import use_multitenant  # noqa: E402

USE_MULTITENANT = use_multitenant()

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
# Building blocks shared by BOTH modes — single source of truth so the two
# INSTALLED_APPS branches below cannot silently drift. Add a shared app HERE, not
# in a branch.
_DJANGO_APPS = [
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
]

_THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
]

_BUSINESS_APPS = [
    "customers",
    "drivers",
    "cars",
    "products",
    "orders",
    "routes",
]

# Standalone base (the larger host project's mode): one default DB, no schemas — NO
# 'tenants' / 'django_tenants'. Multi-tenant REASSEMBLES these blocks into a
# SHARED_APPS+TENANT_APPS union (incl. tenants + django_tenants) in settings_multitenant.py.
INSTALLED_APPS = [*_DJANGO_APPS, *_THIRD_PARTY_APPS, "users", *_BUSINESS_APPS]


# ---------------------------------------------------------------------------
# URL routing / DB routers (standalone base).
#
# Standalone ships a minimal ROOT_URLCONF stub (tenants_back/urls_standalone.py) so the
# app boots and CI can run; at merge the host project supplies its own. No tenant router;
# the plain PostGIS backend (DATABASES below) is the DB engine. Multi-tenant overrides
# ROOT_URLCONF + DATABASE_ROUTERS and adds TENANT_MODEL / PUBLIC_SCHEMA_* / ORIGINAL_BACKEND
# / TENANT_BASE_DOMAINS in settings_multitenant.py.
# ---------------------------------------------------------------------------
ROOT_URLCONF     = "tenants_back.urls_standalone"
DATABASE_ROUTERS = []


# ---------------------------------------------------------------------------
# Worker model: SYNC Gunicorn `sync` (prefork) over WSGI. Not a preference — a
# django-tenants connection carries the tenant's search_path, so correctness needs one
# request per connection per unit of concurrency, which prefork gives by construction.
# The reasoning lives in ONE place: bin/gunicorn_start.sh (`Why not ASGI`); the
# illustrated version is in README "Architecture trade-offs" / docs/why-no-async.*.
#
# Middleware — STANDALONE base (stock session; no tenant/shard middleware, nothing to
# bind a session to on a single DB). Multi-tenant inserts ShardAwareTenantMiddleware +
# TenantShardRoutingMiddleware (+ diagnostics) after CORS and the SchemaBoundSession
# guard after Auth — see settings_multitenant.py for the full ordered list and rationale.
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
# A DECLARATION, not a switch: Django never reads ASGI_APPLICATION (it is a Channels
# setting) and get_asgi_application() does not consult it, so changing this line has no
# effect on anything. What actually selects the stack is gunicorn's worker class + module
# (bin/gunicorn_start.sh, deploy/gunicorn.conf.py). Kept so the intended stack is stated
# next to WSGI_APPLICATION rather than only in deploy scripts.
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
        # Standalone base: plain PostGIS. Multi-tenant overrides ENGINE to the
        # django-tenants backend (+ ORIGINAL_BACKEND) in settings_multitenant.py.
        "ENGINE":             "django.contrib.gis.db.backends.postgis",
        "NAME":               "tenants_back",
        "USER":               "postgres",
        "PASSWORD":           "postgres",
        "HOST":               "127.0.0.1",
        "PORT":               "5432",
        # 0 = close at the end of every request (Django's own default). Persistent
        # connections are an OPT-IN deployment decision, not a base assumption: the count
        # a cluster sees is backend_hosts x gunicorn_workers PER ALIAS (see "Connection
        # sizing" in deploy/DATABASE_SETUP.md), so a non-zero value here would silently
        # multiply by a topology this file knows nothing about — and this same base is
        # what the standalone host project inherits. Production raises it where the
        # topology and Aurora max_connections ARE known: settings_local.py sets
        # CONN_MAX_AGE=60 per alias (see settings_local.py.example).
        "CONN_MAX_AGE":       0,
        # Kept True although it is INERT at CONN_MAX_AGE=0 (there is no reused connection
        # to health-check): it must already be in place for the settings_local override
        # that raises CONN_MAX_AGE, where a stale pooled connection is a real failure mode.
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
    # Standalone base: stock JWT (no tenants to bind to). Multi-tenant overrides
    # DEFAULT_AUTHENTICATION_CLASSES with SchemaBoundJWTAuthentication (rejects a token
    # whose `schema` claim != the request's tenant) in settings_multitenant.py.
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
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

# Multi-tenant adds a dedicated `tenant_resolve` cache (host->Tenant+shard) and the
# TENANT_RESOLVE / TENANT_REGISTRY resolver-config dicts — see settings_multitenant.py.

# API path prefixes — request-handling code that treats API traffic as stateless/JSON:
# the session guard (users.middleware) and error content negotiation (tenants.errors).
# Single source of truth so the two stay in sync.
API_PATH_PREFIXES = ("/api/v1/", "/open_api/api/v1/")

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
# Shared beat schedule — the host project's / business periodic tasks go HERE (they run in
# BOTH modes). Multi-tenant AUGMENTS this dict (adds its own infra entries) rather than
# replacing it — see settings_multitenant.py.
#
# WRAP EVERY ENTRY with commons.platform.beat.scoped_schedule so its SCOPE is explicit (the tenants.E003
# system check enforces this under multi-tenant). Importing scoped_schedule here is safe — no
# circular import; scoped_schedule never reads django.conf.settings (it resolves the mode via
# commons.platform.mode.use_multitenant).
#
# CHOOSING scope — pick by the task's DATA, NOT by the run mode; the SAME value is correct
# for both modes (in standalone every scope simply collapses to "run once"):
#   scope="tenants" — task operates on ONE tenant's data (per-tenant business logic).
#                     standalone: runs ONCE (the single DB). multi-tenant: FANNED OUT to
#                     every ACTIVE tenant. → use this for standalone business tasks too, so
#                     they fan out automatically once multi-tenant is enabled.
#   scope="public"  — global / cross-tenant / infra / public-schema task.
#                     standalone: runs ONCE. multi-tenant: runs ONCE in the public schema.
#
#   from commons.platform.beat import scoped_schedule
#   from celery.schedules import crontab
#
#   CELERY_BEAT_SCHEDULE = {
#       # INTERVAL, per-tenant — every 30s. standalone: once; MT: fanned out to all tenants.
#       "push-updates": scoped_schedule({"task": "apps.x.tasks.push", "schedule": 30.0}, scope="tenants"),
#
#       # CALENDAR, per-tenant — 08:00 in EACH tenant's LOCAL timezone (crontab + tenants =>
#       # tz-aware fanout). fanout_period / grace apply ONLY to this case:
#       #   fanout_period (default 60s) — how often beat polls to catch each tenant's local
#       #       time; the PRECISION / how close to 08:00 local it fires. Smaller = more precise,
#       #       more ticks. Must be <= the schedule's granularity (e.g. <=60s for minute crons).
#       #   grace (default TENANT_BEAT["TZ_GRACE_SECONDS"]=300s) — max LATENESS: if a run was
#       #       missed (beat down) it still fires within `grace`, else it is SKIPPED to the next
#       #       occurrence (never fired late / never N times). Must be >= fanout_period (check
#       #       tenants.E002). Set smaller for interdependent/time-sensitive tasks.
#       "daily-report": scoped_schedule({"task": "apps.x.tasks.daily", "schedule": crontab(minute=0, hour=8)},
#                              scope="tenants", fanout_period=60, grace=300),
#
#       # GLOBAL, once — cross-tenant housekeeping (public schema under MT). fanout_period /
#       # grace are IGNORED for public + for interval entries.
#       "cleanup":      scoped_schedule({"task": "apps.x.tasks.cleanup", "schedule": crontab(minute=0, hour=3)},
#                              scope="public"),
#   }
CELERY_BEAT_SCHEDULE = {}

# Standalone base: no custom queues (Celery's single default "celery" queue, which the
# host project's existing workers already consume) and stock beat (default
# PersistentScheduler / host's choice). Multi-tenant adds the fast/slow/service/fanout
# queues, TENANT_BEAT knobs, CELERY_TASK_TENANT_CACHE_SECONDS, RedBeat
# (CELERY_BEAT_SCHEDULER + CELERY_REDBEAT_*) — see settings_multitenant.py.
# Task-level queues come from commons.platform.beat.task_queue(), which returns None in
# standalone → the default queue (so the fallback cannot leak).


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

