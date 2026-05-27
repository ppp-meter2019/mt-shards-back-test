"""Two AdminSites coexist:

- `public_admin_site` — mounted only on the public host. Tenant administrators
  log in here and manage tenants/domains.
- `admin.site` (the default) — mounted on each tenant host for company
  administrators. Holds business models (cars, orders, ...).

Models that exist in both schemas (User) are registered on BOTH sites; models
that only exist in one schema are registered only where they live.
"""

from django.contrib import admin
from django.contrib.admin import AdminSite
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.models import Group
from django_tenants.admin import TenantAdminMixin

from .models import Domain, Tenant


class PublicAdminSite(AdminSite):
    site_header = "Multi-tenant management"
    site_title = "Multi-tenant management"
    index_title = "Tenant administration"


public_admin_site = PublicAdminSite(name="public_admin")


class TenantAdmin(TenantAdminMixin, admin.ModelAdmin):
    list_display = ("name", "schema_name", "created_on", "is_active")
    search_fields = ("name", "schema_name")


class DomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "tenant", "is_primary")
    search_fields = ("domain",)


# Tenant management exists only on the public schema → only on public admin.
public_admin_site.register(Tenant, TenantAdmin)
public_admin_site.register(Domain, DomainAdmin)
public_admin_site.register(Group, GroupAdmin)