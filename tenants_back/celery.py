"""Project Celery app — uses our shard+schema-aware CeleryApp (tenants.celery).

Config comes from Django settings under the CELERY_ namespace; tasks are
autodiscovered from each app's tasks.py (e.g. tenants/tasks.py).
"""
import os

from tenants.celery import CeleryApp

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tenants_back.settings")

app = CeleryApp("tenants_back")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
