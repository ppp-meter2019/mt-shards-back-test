"""Project Celery app — uses our shard+schema-aware CeleryApp (tenants.celery).

Config comes from Django settings under the CELERY_ namespace; tasks are
autodiscovered from each app's tasks.py (e.g. tenants/tasks.py).
"""
import os

# MUST come BEFORE importing tenants.celery: that pulls in django_tenants.utils,
# whose schema_exists()/schema_rename() default args evaluate
# get_tenant_database_alias() (reads settings.TENANT_DB_ALIAS) at IMPORT time.
# Without DJANGO_SETTINGS_MODULE set first, that raises ImproperlyConfigured
# (e.g. on a clean worker host where the env var isn't exported).
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tenants_back.settings")

from tenants.celery import CeleryApp  # noqa: E402  (must follow the env setup above)

app = CeleryApp("tenants_back")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
