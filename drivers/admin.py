from django.contrib import admin

from .models import Driver


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("user", "license_number", "date_of_birth")
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "license_number",
    )
    autocomplete_fields = ("user",)