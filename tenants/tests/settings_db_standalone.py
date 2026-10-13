"""Standalone (USE_MULTITENANT=0) settings for StandaloneCanaryE2E
(deploy/migrations_plan_rev7.md §20.4): the plain PostGIS backend, one database on the
throwaway server of settings_db.py, and the canary app — so the §8.2 recipe is proven
under Django's own `migrate` too (no migrator, lock_timeout reset TO DEFAULT).

    USE_MULTITENANT=0 DJANGO_SETTINGS_MODULE=tenants.tests.settings_db_standalone \\
        ../p_env/bin/python manage.py test tenants.tests.db_standalone_canary
"""
import os

from django.core.exceptions import ImproperlyConfigured

from commons.platform.mode import use_multitenant

if use_multitenant():
    raise ImproperlyConfigured("settings_db_standalone needs USE_MULTITENANT=0 in the environment.")

from tenants_back.settings import *  # noqa: E402,F401,F403
from tenants_back.settings import CACHES, DATABASES, INSTALLED_APPS  # noqa: E402

DATABASES = {"default": {
    **DATABASES["default"],
    "NAME":         "tm_standalone",
    "USER":         os.environ.get("TEST_PG_USER", "postgres"),
    "PASSWORD":     os.environ.get("TEST_PG_PASSWORD", "postgres"),
    "HOST":         os.environ.get("TEST_PG_HOST", "127.0.0.1"),
    "PORT":         os.environ.get("TEST_PG_PORT", "55432"),
    "CONN_MAX_AGE": 0,
    "OPTIONS":      {"connect_timeout": 5},
    "TEST":         {"NAME": "test_tm_standalone", "TEMPLATE": "template_postgis"},
}}
_REDIS = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379")
for _db_no, _alias in enumerate(sorted(CACHES), start=1):
    CACHES[_alias]["LOCATION"] = f"{_REDIS}/{_db_no}"
INSTALLED_APPS = [*INSTALLED_APPS, "tenants.tests.canary"]
