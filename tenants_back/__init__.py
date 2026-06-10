"""Expose the Celery app so `@shared_task` binds to it and the worker finds it."""
from .celery import app as celery_app

__all__ = ["celery_app"]
