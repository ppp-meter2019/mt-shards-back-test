from django.contrib import admin

from .models import Route


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("name", "driver", "car", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("name",)
    autocomplete_fields = ("driver", "car")
    filter_horizontal = ("orders",)