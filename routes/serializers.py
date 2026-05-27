from django.db import transaction
from rest_framework import serializers

from cars.models import Car
from drivers.models import Driver
from orders.models import Order

from .models import Route


class RouteSerializer(serializers.ModelSerializer):
    driver = serializers.PrimaryKeyRelatedField(queryset=Driver.objects.all())
    car = serializers.PrimaryKeyRelatedField(queryset=Car.objects.all())
    orders = serializers.PrimaryKeyRelatedField(
        queryset=Order.objects.all(), many=True, required=False
    )

    class Meta:
        model = Route
        fields = ["id", "name", "driver", "car", "orders", "status", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        # Same driver / car shouldn't be assigned to two simultaneously-active
        # routes. Soft check — uses status, not time windows.
        status = attrs.get("status", getattr(self.instance, "status", Route.Status.PLANNED))
        if status in {Route.Status.PLANNED, Route.Status.ACTIVE}:
            driver = attrs.get("driver", getattr(self.instance, "driver", None))
            car = attrs.get("car", getattr(self.instance, "car", None))
            qs = Route.objects.filter(
                status__in=[Route.Status.PLANNED, Route.Status.ACTIVE]
            )
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if driver and qs.filter(driver=driver).exists():
                raise serializers.ValidationError(
                    {"driver": "Driver already assigned to another active route."}
                )
            if car and qs.filter(car=car).exists():
                raise serializers.ValidationError(
                    {"car": "Car already assigned to another active route."}
                )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        orders = validated_data.pop("orders", [])
        route = Route.objects.create(**validated_data)
        if orders:
            route.orders.set(orders)
            self._sync_order_status(route)
        return route

    @transaction.atomic
    def update(self, instance, validated_data):
        orders = validated_data.pop("orders", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if orders is not None:
            instance.orders.set(orders)
        self._sync_order_status(instance)
        return instance

    @staticmethod
    def _sync_order_status(route: Route) -> None:
        """Reflect the route status on its orders.

        Why: an admin attaching orders to an active route expects those orders
        to flip to IN_ROUTE in one shot, not in a separate API call.
        """
        if route.status == Route.Status.ACTIVE:
            route.orders.update(status="in_route")
        elif route.status == Route.Status.COMPLETED:
            route.orders.update(status="delivered")