"""Settings for the DB-backed migrator tests (deploy/migrations_plan_rev7.md §20.1).

`default`, `tenant_1`, `tenant_2` are three databases on ONE throwaway PostGIS server,
Redis is a throwaway instance too. Nothing here may reach the deployed Aurora/ElastiCache
that settings_local_multitenant.py points at: DATABASES is rebuilt from scratch and every
cache LOCATION is repointed.

    docker run -d --name tm-pg -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:55432:5432 postgis/postgis:16-3.4
    docker run -d --name tm-redis -p 127.0.0.1:56379:6379 redis:7
    DJANGO_SETTINGS_MODULE=tenants.tests.settings_db \\
        ../p_env/bin/python manage.py test tenants.tests.db_migrator_core

Host/ports can be moved with TEST_PG_HOST / TEST_PG_PORT / TEST_REDIS_URL.
"""
import os

from tenants_back.settings import *  # noqa: F401,F403
from tenants_back.settings import CACHES
from tenants_back.settings_multitenant import _MT_DB_DEFAULTS


def _db(name: str) -> dict:
    return {
        **_MT_DB_DEFAULTS,
        "NAME":         name,
        "USER":         os.environ.get("TEST_PG_USER", "postgres"),
        "PASSWORD":     os.environ.get("TEST_PG_PASSWORD", "postgres"),
        "HOST":         os.environ.get("TEST_PG_HOST", "127.0.0.1"),
        "PORT":         os.environ.get("TEST_PG_PORT", "55432"),
        "CONN_MAX_AGE": 0,
        "OPTIONS":      {"connect_timeout": 5},
        # The postgis image installs the extension into template_postgis, not template1.
        "TEST":         {"NAME": f"test_{name}", "TEMPLATE": "template_postgis"},
    }


DATABASES = {
    "default":  _db("tm_default"),
    "tenant_1": _db("tm_shard1"),
    "tenant_2": _db("tm_shard2"),
}

_REDIS = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379")
# EVERY cache alias, not a list of known ones: a new alias added to the MT settings must not
# silently keep pointing at a deployed instance from the local file.
for _db_no, _alias in enumerate(sorted(CACHES), start=1):
    CACHES[_alias]["LOCATION"] = f"{_REDIS}/{_db_no}"
CELERY_BROKER_URL = CELERY_REDBEAT_REDIS_URL = f"{_REDIS}/0"
CELERY_BROKER_USE_SSL = CELERY_REDBEAT_REDIS_USE_SSL = None

# Per-run migration logs of CLI runs in tests go to a throwaway folder, never into the repo.
import tempfile as _tempfile  # noqa: E402
TENANT_MIGRATE_LOG_DIR = _tempfile.mkdtemp(prefix="tm-migration-logs-")
