"""
Settings for tenants_back — multi-tenant Django/DRF project using django-tenants.

Layout:
- SHARED_APPS  → live in the `public` schema (tenants registry, tenant-admin users).
- TENANT_APPS  → live in each tenant schema (cars, drivers, orders, ...).
- `users` is intentionally in BOTH lists so each schema has its own isolated
  auth_user table; this is what gives tenant-admin / company-admin / customer /
  driver users separate identities per database scope.
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

# Comma-separated list. In dev "*" lets any tenant subdomain through;
# in prod set DJANGO_ALLOWED_HOSTS=".example.com,example.com".
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]

# Django requires CSRF_TRUSTED_ORIGINS for the admin to accept POSTs over
# HTTPS behind a reverse proxy. Accepts schemes: "https://*.example.com".
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

# When nginx terminates TLS we tell Django to trust X-Forwarded-Proto so
# `request.is_secure()` returns True and admin/login pages issue Secure cookies.
if os.environ.get("DJANGO_BEHIND_TLS_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

SHARED_APPS = [
    "django_tenants",
    "tenants",

    "django.contrib.contenttypes",
    "django.contrib.auth",
    # PostGIS: бібліотека з гео-полями + GIS ORM. Без своїх таблиць — їй
    # вистачає одного запису в SHARED_APPS (у TENANT_APPS не дублюємо).
    # Вимагає системних gdal/geos/proj і `CREATE EXTENSION postgis` у БД —
    # див. README §15. Якщо PostGIS поки не налаштовано — закоментуй рядок,
    # інакше Django впаде на імпорті GIS-модулів.
    "django.contrib.gis",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",

    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",

    "users",
]

TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",

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
# django-tenants configuration
# ---------------------------------------------------------------------------

TENANT_MODEL = "tenants.Tenant"
TENANT_DOMAIN_MODEL = "tenants.Domain"

PUBLIC_SCHEMA_URLCONF = "tenants_back.urls_public"
ROOT_URLCONF = "tenants_back.urls_tenant"

DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)

# PostGIS: вказує django-tenants успадковуватись від PostGIS-backend замість
# стандартного PostgreSQL. Дає одночасно мульти-тенантність і GIS типи /
# spatial-індекси / GIS ORM. Вимагає `CREATE EXTENSION postgis` у БД +
# системних gdal/geos/proj — див. README §15. Якщо PostGIS не налаштовано,
# закоментуй цей рядок (тоді django-tenants підхопить дефолт
# `django.db.backends.postgresql`).
ORIGINAL_BACKEND = "django.contrib.gis.db.backends.postgis"


# ---------------------------------------------------------------------------
# Middleware — TenantMainMiddleware MUST be first; it sets the schema based
# on the request hostname before anything else looks at the DB.
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django_tenants.middleware.main.TenantMainMiddleware",
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
# Templates / WSGI
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


# ---------------------------------------------------------------------------
# Database — must be django-tenants' PG backend.
# ---------------------------------------------------------------------------
from django_tenants import postgresql_backend

DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME": os.environ.get("DB_NAME", "tenants_back"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "postgres"),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# DRF + SimpleJWT
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}


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

# Public hostname used when no tenant is matched. Only relevant for explicit
# checks; the actual public schema is determined by tenants.Tenant(schema_name='public').
PUBLIC_SCHEMA_NAME = "public"


# ---------------------------------------------------------------------------
# CORS — only relevant in split-origin dev. In production behind nginx the
# frontend and the API share an origin, so CORS is effectively unused.
# ---------------------------------------------------------------------------

CORS_ALLOW_ALL_ORIGINS = os.environ.get("DJANGO_CORS_ALLOW_ALL", "1") == "1"
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("DJANGO_CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
# Use this to whitelist a wildcard, e.g. all your tenant subdomains:
#   DJANGO_CORS_ALLOWED_ORIGIN_REGEXES=^https://([a-z0-9-]+\.)?example\.com(:\d+)?$
CORS_ALLOWED_ORIGIN_REGEXES = [
    r.strip() for r in os.environ.get("DJANGO_CORS_ALLOWED_ORIGIN_REGEXES", "").split(",") if r.strip()
]
CORS_ALLOW_CREDENTIALS = False


try:
    from .settings_local import *  # noqa: F403
except ImportError:
    print("Can't load local settings!")
