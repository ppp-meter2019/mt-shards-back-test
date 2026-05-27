from django.contrib import admin

from .models import Car


@admin.register(Car)
class CarAdmin(admin.ModelAdmin):
    list_display = ("brand", "model", "year", "license_plate")
    search_fields = ("brand", "model", "license_plate")
    list_filter = ("brand", "year")