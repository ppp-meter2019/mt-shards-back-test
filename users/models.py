from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Single auth model used in every schema. The `role` field decides what
    the user can do, and which schema they should authenticate against:

      - tenant_admin  → public schema (manages Tenant objects)
      - company_admin → tenant schema (manages all business entities)
      - customer      → tenant schema (places orders)
      - driver        → tenant schema (gets routes assigned)

    `username` doubles as the login. Each schema has its own auth_user table,
    so usernames only need to be unique inside a single tenant.
    """

    class Role(models.TextChoices):
        TENANT_ADMIN = "tenant_admin", "Tenant Administrator"
        COMPANY_ADMIN = "company_admin", "Company Administrator"
        CUSTOMER = "customer", "Customer"
        DRIVER = "driver", "Driver"

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CUSTOMER,
    )

    def __str__(self) -> str:
        return f"{self.username} ({self.get_role_display()})"