from django.contrib.gis.db import models as gis_models
from django.core.validators import MinValueValidator
from django.db import models

from customers.models import Customer
from products.models import Product


class Order(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PLACED = "placed", "Placed"
        IN_ROUTE = "in_route", "In route"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PLACED,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Доставка — обидва поля необов'язкові, додані для верифікації PostGIS-зв'язки.
    delivery_address = models.TextField(blank=True, default="")
    delivery_point = gis_models.PointField(geography=True, null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Order #{self.pk} by {self.customer}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )

    class Meta:
        # Don't let the same product be added twice to one order — clients
        # should bump the quantity instead. Avoids accidental double-charging.
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product"], name="unique_product_per_order"
            )
        ]

    def __str__(self) -> str:
        return f"{self.product.name} x{self.quantity}"