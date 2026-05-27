from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


class Driver(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="driver_profile",
    )
    date_of_birth = models.DateField()
    license_number = models.CharField(
        max_length=32,
        unique=True,
        validators=[
            RegexValidator(
                regex=r"^[A-Z0-9\-]{4,32}$",
                message="License number must be 4-32 chars: A-Z, 0-9, dash.",
            )
        ],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user__last_name", "user__first_name")

    def save(self, *args, **kwargs):
        if self.license_number:
            self.license_number = self.license_number.upper().strip()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.user.first_name} {self.user.last_name} ({self.license_number})"