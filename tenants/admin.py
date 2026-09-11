"""Shim for Django's admin autodiscovery.

`django.contrib.admin`'s AppConfig.ready() runs autodiscover_modules("admin"), which imports
`<app_label>.admin` BY CONVENTION — it knows nothing about `tenants.console`. The real
registrations (public_admin_site + its ModelAdmins) live in tenants/console/admin.py, so
without this module they would simply never run and the public admin site would come up
empty. Importing the module is enough: registration is its import side effect.

`public_admin_site` is re-exported so the one name other code needs keeps a stable home —
but new code should import it from `tenants.console.admin` directly.
"""
from tenants.console.admin import public_admin_site  # noqa: F401  (import = registration)
