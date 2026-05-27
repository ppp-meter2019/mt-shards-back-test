from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from tenants.admin import public_admin_site

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


# Each schema has its own auth_user table, so User must be manageable on
# both admin sites.
admin.site.register(User, UserAdmin)
public_admin_site.register(User, UserAdmin)