from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


class Customer(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customer_profile",
    )
    phone = models.CharField(
        max_length=32,
        blank=True,
        validators=[
            RegexValidator(
                regex=r"^\+?[0-9 \-()]{6,32}$",
                message="Phone must be 6-32 chars, digits and +-() space.",
            )
        ],
    )
    address = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("user__last_name", "user__first_name")

    def __str__(self) -> str:
        return self.user.get_full_name() or self.user.username