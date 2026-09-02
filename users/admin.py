from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from commons.platform.admin import management_site

from .models import User


class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Multi-tenant", {"fields": ("role",)}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Multi-tenant", {"fields": ("role",)}),
    )
    list_display = ("username", "email", "role", "is_staff", "is_active")
    list_filter = DjangoUserAdmin.list_filter + ("role",)


# Default site: the single admin in standalone; the per-tenant admin in multitenant
# (each schema has its own auth_user table).
admin.site.register(User, UserAdmin)
# Management (public-host) site — multitenant only; None in standalone.
_mgmt_site = management_site()
if _mgmt_site is not None:
    _mgmt_site.register(User, UserAdmin)