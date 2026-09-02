"""Shared helper for the tenant system checks."""
from functools import wraps

from django.conf import settings


def mt_check(fn):
    """Decorator: make a system check a no-op unless USE_MULTITENANT.

    The beat/fanout/middleware invariants only apply in multi-tenant mode; in standalone the
    `tenants` app isn't installed and none of them are meaningful. This turns 'MT-only' into a
    declarative label instead of a guard clause repeated in every check. Place it BELOW
    @register() so Celery/Django registers the wrapper:

        @register()
        @mt_check
        def my_check(app_configs, **kwargs): ...
    """
    @wraps(fn)
    def wrapper(app_configs, **kwargs):
        if not getattr(settings, "USE_MULTITENANT", False):
            return []
        return fn(app_configs, **kwargs)
    return wrapper
