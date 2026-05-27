import datetime

from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models


def _max_year() -> int:
    return datetime.date.today().year + 1


class Car(models.Model):
    brand = models.CharField(max_length=80)
    model = models.CharField(max_length=80)
    year = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1900), MaxValueValidator(_max_year())]
    )
    license_plate = models.CharField(
        max_length=16,
        unique=True,
        validators=[
            RegexValidator(
                regex=r"^[A-Z0-9\- ]{4,16}$",
                message="License plate must be 4-16 chars: A-Z, 0-9, dash or space.",
            )
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("brand", "model")

    def save(self, *args, **kwargs):
        # Normalize plates so "aa 1234 bb" and "AA1234BB" don't sneak past the
        # uniqueness check as different rows.
        if self.license_plate:
            self.license_plate = self.license_plate.upper().strip()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.brand} {self.model} [{self.license_plate}]"