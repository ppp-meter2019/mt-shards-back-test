from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin

from .models import Order, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 1
    autocomplete_fields = ("product",)


@admin.register(Order)
class OrderAdmin(GISModelAdmin):
    """
    GISModelAdmin замість звичайного admin.ModelAdmin — щоб delivery_point
    рендерився як OpenLayers-мапа, а не сирий WKT-input. Решта полів (FK,
    статус, текстова адреса) поводиться як зазвичай.
    """

    list_display = ("id", "customer", "status", "delivery_address", "created_at")
    list_filter = ("status",)
    search_fields = ("customer__user__username", "delivery_address")
    autocomplete_fields = ("customer",)
    inlines = [OrderItemInline]