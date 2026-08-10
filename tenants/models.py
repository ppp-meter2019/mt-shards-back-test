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

    # Human-readable company label. Unique + required, but NOT an identifier:
    # every lookup/routing uses schema_name. Renamed from `name` to remove the
    # name-vs-schema_name ambiguity.
    company_name      = models.CharField(max_length=120, unique=True)
    # Free-text notes; optional, purely descriptive.
    description       = models.TextField(blank=True)
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
        return self.company_name

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
            # On create, reject a schema_name that collides with a reserved global
            # subdomain label (same source as Domain validation). Create-only:
            # schema_name is immutable, and we must not fail edits of an existing row.
            if self.pk is None:
                from .validators import validate_tenant_schema_name
                validate_tenant_schema_name(self.schema_name)

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

    def clean(self):
        """Validate format + reserved-host rules for business-tenant domains.

        The public/management tenant is EXEMPT: its hosts are set by
        bootstrap_public and may legitimately be a bare/base host. This runs on
        the admin form (full_clean) and any explicit full_clean() call;
        operator-trusted management commands use .create() and bypass it, matching
        how Tenant/Shard creation bypass their own clean().
        """
        super().clean()
        from .validators import validate_tenant_domain
        if self.tenant_id and self.tenant.schema_name == get_public_schema_name():
            return
        self.domain = validate_tenant_domain(self.domain)


class ReservedHostRule(models.Model):
    """Operator-managed rule that forbids a host/subdomain from being claimed by a
    business tenant. Lives in the public schema (shared app), managed from the
    public admin site and the management API. Checked by
    tenants.validators.validate_tenant_domain on Domain create.

    Deny-only (no allow-exceptions): a host is reserved iff at least one active
    rule matches it. See the reserved-host design discussion.
    """

    class MatchType(models.TextChoices):
        EXACT  = "exact",  "Exact host"                     # manage.routegenie.com
        SUFFIX = "suffix", "Domain suffix (host and all subdomains)"  # *.internal.example.com
        LABEL  = "label",  "Subdomain label"                # leading label, optionally under a base

    match_type  = models.CharField(max_length=16, choices=MatchType.choices)
    # EXACT/SUFFIX: a hostname. LABEL: a single DNS label (e.g. "www").
    value       = models.CharField(max_length=253)
    # LABEL only: restrict the rule to hosts under this base domain. Blank => the
    # label is reserved GLOBALLY (any host whose leading label matches).
    base_domain = models.CharField(max_length=253, blank=True)
    is_active   = models.BooleanField(default=True)
    note        = models.CharField(max_length=200, blank=True)
    created_on  = models.DateTimeField(auto_now_add=True)
    modified    = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["match_type", "value"]
        constraints = [
            models.UniqueConstraint(
                fields=["match_type", "value", "base_domain"],
                name="tenants_reservedhostrule_unique",
            ),
        ]

    def __str__(self):
        if self.match_type == self.MatchType.LABEL:
            scope = f" under {self.base_domain}" if self.base_domain else " (global)"
            return f"label '{self.value}'{scope}"
        return f"{self.match_type} '{self.value}'"

    @classmethod
    def normalize(cls, match_type, value, base_domain=""):
        """Normalize + validate (value, base_domain) for a match_type; return the pair.

        SINGLE source of truth for rule normalization, shared by clean() (admin) and
        ReservedHostRuleSerializer.validate() (API) so the two enforcement paths can
        never drift. Raises django ValidationError on bad input. base_domain is
        meaningful only for LABEL rules; it is forced to "" otherwise.
        """
        from .validators import validate_hostname, validate_label
        if match_type == cls.MatchType.LABEL:
            value = validate_label(value)
            base_domain = validate_hostname(base_domain) if (base_domain or "").strip() else ""
        elif match_type in (cls.MatchType.EXACT, cls.MatchType.SUFFIX):
            value = validate_hostname(value)
            base_domain = ""
        else:
            raise ValidationError(f"Unknown match type: {match_type!r}.")
        return value, base_domain

    def clean(self):
        """Normalize + validate value/base_domain according to match_type."""
        super().clean()
        self.value, self.base_domain = self.normalize(
            self.match_type, self.value, self.base_domain)

    def matches(self, host: str) -> bool:
        """Whether this rule reserves `host`. Shared by the domain validator and the
        management API's conflict-preview action so both agree exactly."""
        from .validators import normalize_host
        host = normalize_host(host)
        val = normalize_host(self.value)
        if self.match_type == self.MatchType.EXACT:
            return host == val
        if self.match_type == self.MatchType.SUFFIX:
            return host == val or host.endswith("." + val)
        if self.match_type == self.MatchType.LABEL:
            if host.split(".", 1)[0] != val:
                return False
            base = normalize_host(self.base_domain)
            return not base or host == base or host.endswith("." + base)
        return False

    def candidate_q(self):
        """A Q() returning a SUPERSET of the domains this rule matches — cheap to run
        in SQL so matches() (the authority) confirms only a narrowed set.

        Contract: MUST NOT exclude any true match; over-inclusion is fine (matches()
        drops it). Lookups are case-INSENSITIVE on purpose: CLI-created domains may be
        stored non-normalized, and matches() compares lower-cased — a case-sensitive
        prefilter would miss them and break the superset guarantee.
        """
        from django.db.models import Q
        from .validators import normalize_host
        val = normalize_host(self.value)
        if self.match_type == self.MatchType.EXACT:
            return Q(domain__iexact=val)
        if self.match_type == self.MatchType.SUFFIX:
            return Q(domain__iexact=val) | Q(domain__iendswith="." + val)
        if self.match_type == self.MatchType.LABEL:
            # leading label == val; the base (if any) is confirmed by matches().
            return Q(domain__iexact=val) | Q(domain__istartswith=val + ".")
        return Q(pk__in=[])       # unknown type → nothing

    def denial_message(self, host: str) -> str:
        if self.match_type == self.MatchType.LABEL:
            if self.base_domain:
                return f"Subdomain '{self.value}' is reserved under {self.base_domain}."
            return f"Subdomain '{self.value}' is reserved."
        if self.match_type == self.MatchType.SUFFIX:
            return f"Hosts under '{self.value}' are reserved."
        return f"Host '{self.value}' is reserved."
