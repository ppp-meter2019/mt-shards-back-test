"""Facade for the platform/management admin site.

`management_site()` returns the tenant-management admin site in multitenant mode (the
public-host `AdminSite`), or `None` in standalone — where there is no separate
management site, so callers register on the default `admin.site` only.

Usage (register a model on the default site always + the management site when present):

    admin.site.register(Model, ModelAdmin)
    if (site := management_site()) is not None:
        site.register(Model, ModelAdmin)
"""
from django.conf import settings


def management_site():
    if settings.USE_MULTITENANT:
        from tenants.admin import public_admin_site   # lazy: absent in standalone
        return public_admin_site
    return None
