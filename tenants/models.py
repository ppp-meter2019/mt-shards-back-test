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

These models are never what a request or a task holds: `request.tenant` and the worker's
tenant are a tenants.resolver.TenantSnapshot, which has no save()/delete() to guard.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Lower, Trim
from django.utils import timezone
from django_tenants.models import DomainMixin, TenantMixin
from django_tenants.utils import get_public_schema_name


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

    def __str__(self) -> str:
        return f"{self.name or self.alias} [{self.alias}]"

    def clean(self) -> None:
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

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        """Protect the default shard.

        Combined with Tenant.shard on_delete=PROTECT, a real shard can only be
        deleted if it has no tenants AND is not the default shard.
        """
        if self.is_default:
            raise ProtectedError(
                "Default shard cannot be deleted - it is reserved for the public schema.",
                set(),
            )
        return super().delete(*args, **kwargs)


def _validate_timezone(value: str | None) -> None:
    """Validate an IANA timezone name; NULL/empty is allowed (the 'unset' sentinel)."""
    if not value:
        return
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError({"timezone": f"{value!r} is not a valid IANA timezone."})


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
    # every lookup/routing uses schema_name. Named `company_name` rather than `name` so the
    # display label is never mistaken for the identifier at a call site.
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
    # When the STATUS last moved — not when the row was last touched. auto_now would mean
    # the latter: it fires on every .save() (an admin edit of `description` would shift it)
    # and never on QuerySet.update() (where every status writer lives). The value is now
    # maintained by save() below for .save() paths, and set explicitly by the .update()
    # callers (migrate_schemas, reconcile_tenants, console.views.TenantViewSet._transition).
    status_changed_at = models.DateTimeField(default=timezone.now)
    last_error        = models.TextField(blank=True)
    created_on        = models.DateField(auto_now_add=True)
    # IANA timezone for per-tenant CALENDAR (crontab) tasks. NULL = no-op sentinel set
    # at creation; the real value is pushed onto this public row by an in-schema settings
    # singleton (arrives at merge) via sync_tenant_timezone(). While NULL, calendar tasks
    # are NOT scheduled for this tenant (interval tasks are unaffected). See
    # deploy/celery_fanout_design.md §3.
    timezone          = models.CharField(
        max_length=64, null=True, blank=True, default=None, validators=[_validate_timezone])

    # Schema lifecycle is fully managed by our management commands.
    auto_create_schema = False
    auto_drop_schema   = False

    class Meta:
        indexes = [
            # Both beat reads served by ONE partial, covering index:
            #   interval fanout  ->  WHERE status='active' AND schema_name <> 'public'
            #   calendar fanout  ->  ... AND timezone IS NOT NULL
            # (commons.platform.tenancy.active_target_schemas / active_tenants_with_tz —
            # re-read fresh on EVERY tick, deliberately uncached, so this is the read that
            # repeats forever.)
            #
            # PARTIAL on status: the index holds only the rows beat ever looks at.
            # COVERING (schema_name, timezone): those are the only two columns either query
            # selects, so both are index-ONLY scans — the second predicate and the public
            # exclusion are both answered from the index key without touching the heap.
            #
            # Conditioning on `timezone IS NOT NULL` instead would serve the calendar query
            # and leave the interval one on a seq scan; conditioning on status serves both.
            #
            # NB the condition is the LITERAL "active", not Status.ACTIVE: a nested class
            # body does not see the enclosing class body's namespace, so the enum name is
            # unresolvable here (and `Tenant` is not bound yet either). It is also what the
            # migration serializes either way. test_models pins the two together.
            #
            # NB2 PostgreSQL uses a partial index only when it can PROVE the query predicate
            # implies the index predicate. `status = 'active'` matches literally; a future
            # `status__in=[ACTIVE, ...]` would not, and the index would silently go unused.
            models.Index(
                fields=["schema_name", "timezone"],
                name="tenants_tenant_active_idx",
                condition=models.Q(status="active"),
            ),
        ]

    def __str__(self) -> str:
        return self.company_name

    @property
    def db_alias(self) -> str:
        return self.shard.alias

    def clean(self) -> None:
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
            # On create, validate the schema_name FORMAT and the reserved-label rules with
            # the same validators the API serializer uses, so the admin and the API cannot
            # disagree about what a valid tenant is. This is the ONLY format check on the
            # admin path: the model field itself carries django-tenants' `^(?!pg_).{1,63}$`,
            # which accepts spaces, quotes and non-ASCII — and schema_name from here reaches
            # `CREATE SCHEMA "{...}"` in migrate_schemas.
            # Create-only: schema_name is immutable (read-only on the admin change form), and
            # re-validating would block edits of a row whose name predates the convention.
            # NB assigns the NORMALIZED name back ("Foo-Bar" -> "foo_bar").
            if self.pk is None:
                from .validators import validate_schema_name, validate_tenant_schema_name
                self.schema_name = validate_schema_name(self.schema_name)
                validate_tenant_schema_name(self.schema_name)

    @classmethod
    def from_db(cls, db: str | None, field_names: Sequence[str],
                values: Sequence[Any]) -> "Tenant":
        """Snapshot the status as loaded, so save() can tell a real transition from any
        other edit. Guarded on field_names because a deferred load (.only()/.defer())
        would otherwise trigger a refetch right here."""
        obj = super().from_db(db, field_names, values)
        if "status" in field_names:
            obj._loaded_status = obj.status
        return obj

    def save(self, *args: Any, **kwargs: Any) -> None:
        # status_changed_at tracks the STATUS, not the row — so stamp it here iff the status
        # actually moved. This keeps every .save() path correct (the admin status change
        # included) without each caller remembering, and stops an unrelated edit
        # (description, company_name, timezone) from shifting it the way auto_now did.
        # QuerySet.update() cannot be intercepted here, so those callers set it explicitly.
        # No _loaded_status (a deferred load) => stamp: a MISSED transition is worse than a
        # spurious timestamp. update_fields is keyword-only in Django 5.x, so kwargs is the
        # whole story; it must be extended or the new value would not be written at all
        # (e.g. scripts/resolve_cache_bench.py does save(update_fields=["status"])).
        if self._state.adding or self.status != getattr(self, "_loaded_status", None):
            self.status_changed_at = timezone.now()
            if (update_fields := kwargs.get("update_fields")) is not None:
                kwargs["update_fields"] = {*update_fields, "status_changed_at"}
        return super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        """Protect the public tenant."""
        if self.schema_name == get_public_schema_name():
            raise ProtectedError(
                "Public tenant cannot be deleted - it is required by django-tenants "
                "to route requests on the public host.",
                set(),
            )
        return super().delete(*args, **kwargs)


class Domain(DomainMixin):
    """hostname -> tenant mapping. Resolved every request by ShardAwareTenantMiddleware."""

    class Meta:
        constraints = [
            # The column is CANONICAL by construction: lower-cased, whitespace-trimmed, no
            # trailing dot — exactly the fixed points of validators.normalize_host.
            #
            # Two things depend on it. (1) Reachability: request.get_host() returns the raw
            # header and this column compares exactly, so a non-canonical row is a tenant
            # nobody can reach — better to refuse the INSERT than to serve nothing.
            # (2) ReservedHostRule.candidate_q() compares the column DIRECTLY, which is
            # sound only while this holds; without it that method silently excludes true
            # matches and the `conflicts` action under-reports.
            #
            # Domain.save() normalizes, but bulk_create() / QuerySet.update() / raw SQL
            # bypass it — this is what makes the invariant hold on those paths too. NB
            # save() can still produce a rejected value for pathological input
            # ("acme.com ." normalizes to "acme.com "), and that INSERT failing loudly is
            # the intended outcome; validate_hostname refuses such input earlier anyway.
            models.CheckConstraint(
                condition=models.Q(
                    domain=models.Func(
                        Lower(Trim("domain")), models.Value("."), function="RTRIM")),
                name="tenants_domain_canonical",
            ),
        ]

    def clean(self) -> None:
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

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Canonicalize the hostname on EVERY save path, then defer to DomainMixin.

        clean() is not enough: it only runs under full_clean() (admin form, explicit
        calls), while .create()/.save() from operator-trusted commands, data migrations
        and legacy imports bypass it by design. A non-canonical value (upper case,
        trailing FQDN dot, copy-pasted whitespace) is stored verbatim and can then never
        match an incoming Host - request.get_host() returns the RAW header and the
        column compares case-sensitively - so the tenant is silently unreachable.

        Normalization is NOT validation: this deliberately does not apply the
        reserved-host rules, so the public tenant's exemption in clean() still holds.
        NB: bulk_create() and QuerySet.update() bypass save() entirely. get_or_create()
        DOES reach save() on the create branch, but its lookup half matches on the RAW
        kwarg - so a non-canonical value would miss the existing row, then collide with
        it on the unique index. Callers on that path must normalize the lookup value
        themselves (see bootstrap_public).
        """
        from .validators import normalize_host
        self.domain = normalize_host(self.domain)
        return super().save(*args, **kwargs)


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

    def __str__(self) -> str:
        if self.match_type == self.MatchType.LABEL:
            scope = f" under {self.base_domain}" if self.base_domain else " (global)"
            return f"label '{self.value}'{scope}"
        return f"{self.match_type} '{self.value}'"

    @classmethod
    def normalize(cls, match_type: str, value: str, base_domain: str = "") -> tuple[str, str]:
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

    def clean(self) -> None:
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

    def candidate_q(self) -> models.Q:
        """A Q() returning a SUPERSET of the domains this rule matches — cheap to run
        in SQL so matches() (the authority) confirms only a narrowed set.

        Contract: MUST NOT exclude any true match; over-inclusion is fine (matches()
        drops it). Compares the column DIRECTLY — case-sensitively, with no trailing-dot
        handling — and that is sound ONLY because the tenants_domain_canonical CHECK
        constraint guarantees every stored domain is already a fixed point of
        normalize_host, which is what matches() reduces its argument to. Weaken that
        constraint and this method starts silently excluding true matches.
        """
        from django.db.models import Q
        from .validators import normalize_host
        val = normalize_host(self.value)
        if self.match_type == self.MatchType.EXACT:
            return Q(domain=val)
        if self.match_type == self.MatchType.SUFFIX:
            return Q(domain=val) | Q(domain__endswith="." + val)
        if self.match_type == self.MatchType.LABEL:
            # leading label == val; the base (if any) is confirmed by matches().
            return Q(domain=val) | Q(domain__startswith=val + ".")
        return Q(pk__in=[])       # unknown type → nothing

    def denial_message(self, host: str) -> str:
        if self.match_type == self.MatchType.LABEL:
            if self.base_domain:
                return f"Subdomain '{self.value}' is reserved under {self.base_domain}."
            return f"Subdomain '{self.value}' is reserved."
        if self.match_type == self.MatchType.SUFFIX:
            return f"Hosts under '{self.value}' are reserved."
        return f"Host '{self.value}' is reserved."


class TaskRun(models.Model):
    """Durable per-(task, args, tenant-schema) last-run watermark for CALENDAR (tz) fanout.

    Lives in default.public (SHARED). Makes per-tenant due-ness level-triggered: a missed
    tick self-heals within `grace`, and re-firing the same occurrence is deduped. Only
    calendar tasks write here; interval / public tasks do not. See
    deploy/celery_fanout_design.md §3.

    `args_sig` is part of the identity because two schedule entries may share a task NAME
    and differ only by args — `fetch(1)` at 08:00 and `fetch(7)` at 09:00 are two
    independent schedules. The fanout overlap-lock already discriminates on the same axis
    (tenants.celery.dispatch.argsig); the watermark must agree with it, or the entry whose
    wave lands second reads the first one's watermark, sees the occurrence as already run,
    and is skipped forever. It is a short digest rather than the raw args so the column
    stays bounded and indexable, and "" for the no-args majority so those rows read as
    plainly as they did before the column existed.
    """
    schema      = models.CharField(max_length=63)
    task        = models.CharField(max_length=255)
    # tenants.celery.dispatch.argsig(task_args): a 12-char digest, or "" for the common
    # no-args entry — so the rows an operator reads by hand stay readable and only an entry
    # that actually carries args gets a discriminator. Never NULL.
    args_sig    = models.CharField(max_length=12, blank=True, default="")
    last_run_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["schema", "task", "args_sig"],
                                    name="tenants_taskrun_unique"),
        ]
        # `task` alone, not the full key: this index serves operator queries ("why did
        # x.report not fire for tenant Y") across every args variant of a task.
        indexes = [models.Index(fields=["task"], name="tenants_taskrun_task_idx")]

    def __str__(self) -> str:
        # The signature is shown only when there is one — for a no-args entry ("") it would
        # be a constant in the operator's face on every row.
        sig = f"[{self.args_sig}]" if self.args_sig else ""
        return f"{self.task}{sig}@{self.schema} last={self.last_run_at:%Y-%m-%d %H:%M:%SZ}"

    @classmethod
    def load_map(cls, task: str, args_sig: str) -> dict[str, datetime]:
        """{schema: last_run_at} for ONE schedule entry — one query per tick. Scoped by
        args_sig as well as task: another entry for the same task with different args keeps
        its own watermark."""
        return dict(
            cls.objects.filter(task=task, args_sig=args_sig)
                       .values_list("schema", "last_run_at")
        )

    @classmethod
    def mark_ran(cls, task: str, args_sig: str, schemas: Iterable[str],
                 run_ts: datetime | str) -> None:
        """Bulk-upsert last_run_at=run_ts for the given schemas (after successful send)."""
        if not schemas:
            return
        if isinstance(run_ts, str):
            from django.utils.dateparse import parse_datetime
            run_ts = parse_datetime(run_ts)
        cls.objects.bulk_create(
            [cls(schema=s, task=task, args_sig=args_sig, last_run_at=run_ts)
             for s in schemas],
            update_conflicts=True,
            unique_fields=["schema", "task", "args_sig"],
            update_fields=["last_run_at"],
        )


def sync_tenant_timezone(schema_name: str, tz: str | None) -> int:
    """Set a tenant's IANA timezone on its PUBLIC Tenant row (default.public).

    The single writer of Tenant.timezone — intended for the in-schema settings singleton's
    save hook (arrives at project merge). Uses .update() (no signals); the fanout dispatcher
    reads tz fresh each tick, so there is no cache to invalidate. Returns rows updated (0 if
    the schema is unknown). Pass tz=None to reset a tenant back to the 'unset' sentinel.
    """
    return Tenant.objects.filter(schema_name=schema_name).update(timezone=tz)
