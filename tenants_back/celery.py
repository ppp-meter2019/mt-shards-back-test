"""Project Celery app — uses our shard+schema-aware CeleryApp (tenants.celery).

Config comes from Django settings under the CELERY_ namespace; tasks are
autodiscovered from each app's tasks.py (e.g. tenants/tasks.py).
"""
import os

# DJANGO_SETTINGS_MODULE MUST be set before touching settings / importing tenants.celery:
# reading settings resolves USE_MULTITENANT, and (multitenant) importing tenants.celery
# pulls in django_tenants.utils, whose schema_exists()/schema_rename() default args
# evaluate get_tenant_database_alias() (reads settings.TENANT_DB_ALIAS) at IMPORT time —
# without the env var that raises ImproperlyConfigured on a clean worker host.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tenants_back.settings")

from django.conf import settings  # noqa: E402

# Mode gate: multitenant uses the shard+schema-aware CeleryApp (per-task tenant_context
# via TenantTask.__call__); standalone uses a plain Celery app (single DB, no schema
# headers, no context switch). This also means standalone NEVER imports tenants.celery /
# django_tenants — so django-tenants is not required to boot Celery there.
if settings.USE_MULTITENANT:
    from tenants.celery import CeleryApp  # noqa: E402
    app = CeleryApp("tenants_back")
else:
    from celery import Celery  # noqa: E402
    app = Celery("tenants_back")

app.config_from_object("django.conf:settings", namespace="CELERY")

if settings.USE_MULTITENANT:
    # RedBeat's is_key_in_conf() tests membership with `in`, which does NOT report keys
    # loaded via the CELERY_ namespace (a Celery ConfigurationView quirk) — so RedBeat
    # emits a spurious "set redbeat_redis_url explicitly" deprecation even though the
    # value IS present and used. Set it directly so the membership check passes.
    app.conf.redbeat_redis_url = settings.CELERY_REDBEAT_REDIS_URL

app.autodiscover_tasks()
