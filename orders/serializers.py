from django.db import transaction
from rest_framework import serializers

from customers.models import Customer
from products.models import Product

from .models import Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_price = serializers.DecimalField(
        source="product.price", max_digits=10, decimal_places=2, read_only=True
    )

    class Meta:
        model = OrderItem
        fields = ["id", "product", "product_name", "product_price", "quantity"]
        read_only_fields = ["id", "product_name", "product_price"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True)
    customer = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.all(),
        required=False,  # customers don't need to set this — we infer it
    )

    class Meta:
        model = Order
        fields = ["id", "customer", "status", "created_at", "items"]
        read_only_fields = ["id", "created_at"]

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Order must contain at least one item.")
        seen = set()
        for item in items:
            product = item["product"]
            if product.pk in seen:
                raise serializers.ValidationError(
                    f"Product '{product.name}' listed more than once — bump quantity instead."
                )
            seen.add(product.pk)
        return items

    @transaction.atomic
    def create(self, validated_data):
        items_data = validated_data.pop("items")
        order = Order.objects.create(**validated_data)
        OrderItem.objects.bulk_create(
            [OrderItem(order=order, **item) for item in items_data]
        )
        return order

    @transaction.atomic
    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if items_data is not None:
            # Replace the basket entirely — simpler than diff/merge and matches
            # how a typical "edit my order" UI would PUT it back.
            instance.items.all().delete()
            OrderItem.objects.bulk_create(
                [OrderItem(order=instance, **item) for item in items_data]
            )
        return instance