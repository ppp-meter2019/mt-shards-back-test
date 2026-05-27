from django.db import models

from cars.models import Car
from drivers.models import Driver
from orders.models import Order


class Route(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    name = models.CharField(max_length=120)
    driver = models.ForeignKey(
        Driver, on_delete=models.PROTECT, related_name="routes"
    )
    car = models.ForeignKey(
        Car, on_delete=models.PROTECT, related_name="routes"
    )
    orders = models.ManyToManyField(Order, related_name="routes", blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PLANNED,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Route '{self.name}' [{self.driver} / {self.car}]"