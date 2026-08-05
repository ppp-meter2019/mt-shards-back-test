"""
Models:
    Shard   - physical Aurora cluster registry (one row per settings.DATABASES alias)
    Tenant  - business tenant with FK to Shard and status state machine
    Domain  - hostname -> tenant mapping (django-tenants)

Status transitions are enforced by:
  - Tenant.clean()                  (validation)
  - TenantAdminForm                 (UI)
  - migrate_schemas command         (atomic claim + state machine)
  - reconcile_tenants command       (manual recovery)

Delete protections:
  - Tenant.shard FK is on_delete=PROTECT  (shard with tenants cannot be removed)
  - Shard.delete()  blocks deletion of the default shard
  - Tenant.delete() blocks deletion of the public tenant
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.deletion import ProtectedError
from django_tenants.models import DomainMixin, TenantMixin
from django_tenants.utils import get_public_schema_name


class ReadOnlyInstanceError(RuntimeError):
    """Raised when code tries to save()/delete() a request-scoped, read-only Tenant
    or Shard. tenants.middleware marks request.tenant (and its .shard) read-only —
    whether resolved fresh OR rebuilt from the resolution cache — because it is a
    routing snapshot, not a handle for mutating the registry. Re-fetch a fresh
    instance (Model.objects.get(pk=...)) to persist changes. Subclasses RuntimeError
    so existing `except RuntimeError` handlers still catch it."""


class Shard(models.Model):
    """A physical Aurora cluster that can host tenant schemas.

    `alias` must be a key declared in settings.DATABASES at startup.
    Exactly one Shard has is_default=True; it hosts only the public schema.
    """

    alias      = models.CharField(max_length=64, unique=True)
    name       = models.CharField(max_length=120, blank=True)
    is_default = models.BooleanField(default=False)
    is_active  = models.BooleanField(default=True)
    created_on = models.DateTimeField(auto_now_add=True)
    modified   = models.DateTimeField(auto_now=True)

    # Set True by tenants.middleware on request.tenant / its .shard — a read-only
    # routing snapshot (resolved fresh OR rebuilt from the resolution cache). Saving
    # it would clobber the real row (a cache-rebuilt instance carries only cached
    # fields). Plain attribute, not a model field: no column, no migration.
    read_only = False

    class Meta:
        # Partial unique index: at most one row with is_default=True.
        # Rows with is_default=False are not included in the index.
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="tenants_only_one_default_shard",
            ),
        ]

    def __str__(self):
        return f"{self.name or self.alias} [{self.alias}]"

    def clean(self):
        super().clean()
        if self.pk is not None:
            old_alias = Shard.objects.filter(pk=self.pk).values_list("alias", flat=True).first()
            if old_alias is not None and old_alias != self.alias:
                raise ValidationError({
                    "alias": "Shard alias is immutable once set (it maps to a settings.DATABASES key)."
                })
        if self.alias not in settings.DATABASES:
            raise ValidationError({
                "alias": (
                    f"Alias {self.alias!r} is not in settings.DATABASES. "
                    f"Available: {sorted(settings.DATABASES)}"
                )
            })
        if self.is_default and self.alias != "default":
            raise ValidationError({"alias": "Default shard must use the 'default' database alias."})
        if not self.is_default and self.alias == "default":
            raise ValidationError({
                "alias": "The 'default' database is reserved for the public schema."
            })

    def save(self, *args, **kwargs):
        if self.read_only:
            raise ReadOnlyInstanceError(
                "This Shard is a read-only request snapshot (tenants.middleware); "
                "re-fetch it via Shard.objects.get(pk=...) before saving."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Refuse to delete a read-only request snapshot; protect the default shard.

        Combined with Tenant.shard on_delete=PROTECT, a real shard can only be
        deleted if it has no tenants AND is not the default shard.
        """
        if self.read_only:
            raise ReadOnlyInstanceError(
                "This Shard is a read-only request snapshot; re-fetch it via "
                "Shard.objects.get(pk=...) before deleting (would remove the real row)."
            )
        if self.is_default:
            raise ProtectedError(
                "Default shard cannot be deleted - it is reserved for the public schema.",
                set(),
            )
        return super().delete(*args, **kwargs)


class Tenant(TenantMixin):
    """A business tenant. Owns one PostgreSQL schema on one Shard.

    Status state machine - migrations transition the status atomically.
    Migratable statuses: NEW, ACTIVE, DEACTIVATED.
    Non-migratable: PENDING (claimed by another process), FAILED (admin attention).
    """

    class Status(models.TextChoices):
        NEW         = "new",         "New (not yet migrated)"
        PENDING     = "pending",     "Pending migration"
        ACTIVE      = "active",      "Active"
        DEACTIVATED = "deactivated", "Deactivated"
        FAILED      = "failed",      "Failed"

    name              = models.CharField(max_length=120, unique=True)
    shard             = models.ForeignKey(
        "tenants.Shard",
        on_delete=models.PROTECT,
        related_name="tenants",
    )
    status            = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    previous_status   = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    status_changed_at = models.DateTimeField(auto_now=True)
    last_error        = models.TextField(blank=True)
    created_on        = models.DateField(auto_now_add=True)

    # Schema lifecycle is fully managed by our management commands.
    auto_create_schema = False
    auto_drop_schema   = False

    # See Shard.read_only.
    read_only = False

    def __str__(self):
        return self.name

    @property
    def db_alias(self) -> str:
        return self.shard.alias

    def clean(self):
        """Enforce: public schema on default shard; business tenants on non-default."""
        super().clean()
        public = get_public_schema_name()
        if self.schema_name == public:
            if not self.shard.is_default:
                raise ValidationError({"shard": "Public schema must be on the default shard."})
        else:
            if self.shard.is_default:
                raise ValidationError({"shard": "Business tenants cannot live on the default shard."})
            if not self.shard.is_active:
                raise ValidationError({"shard": "Selected shard is not active."})

    def save(self, *args, **kwargs):
        if self.read_only:
            raise ReadOnlyInstanceError(
                "This Tenant is a read-only request snapshot (tenants.middleware); "
                "re-fetch it via Tenant.objects.get(pk=...) before saving."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Refuse to delete a read-only request snapshot; protect the public tenant."""
        if self.read_only:
            raise ReadOnlyInstanceError(
                "This Tenant is a read-only request snapshot; re-fetch it via "
                "Tenant.objects.get(pk=...) before deleting (would remove the real row)."
            )
        if self.schema_name == get_public_schema_name():
            raise ProtectedError(
                "Public tenant cannot be deleted - it is required by django-tenants "
                "to route requests on the public host.",
                set(),
            )
        return super().delete(*args, **kwargs)


class Domain(DomainMixin):
    """hostname -> tenant mapping. Resolved every request by ShardAwareTenantMiddleware."""
