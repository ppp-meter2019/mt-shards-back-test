from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class Tenant(TenantMixin):
    """A company served by the platform (alpha / beta / gamma / ...).

    Each Tenant owns its own PostgreSQL schema; django-tenants creates the
    schema and runs `TENANT_APPS` migrations against it on save when
    `auto_create_schema = True`.
    """

    name = models.CharField(max_length=120, unique=True)
    created_on = models.DateField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    auto_create_schema = True
    auto_drop_schema = True

    def __str__(self) -> str:
        return self.name


class Domain(DomainMixin):
    """Maps a hostname (e.g. `alpha.localhost`) to a tenant."""
    pass