"""Two AdminSites coexist:

- `public_admin_site` - mounted only on the public host (urls_public.py).
  Tenant administrators manage shards, tenants, domains and platform users.
- `admin.site` (default) - mounted on each tenant host (urls_tenant.py).
  Company administrators manage business models (cars, orders, ...).

Models that exist in both schemas (User) are registered on BOTH sites;
models that only exist in one schema are registered only where they live.
"""

from typing import Any

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import AdminSite
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.utils import timezone
from django_tenants.admin import TenantAdminMixin
from django_tenants.utils import get_public_schema_name

# Runtime layer — console -> tenants is the allowed direction.
from tenants import migrator
from tenants.models import Domain, ReservedHostRule, Shard, Tenant
from tenants.resolver import resolve_cache


class PublicAdminSite(AdminSite):
    site_header = "Multi-tenant management"
    site_title  = "Multi-tenant management"
    index_title = "Tenant administration"


public_admin_site = PublicAdminSite(name="public_admin")


# ---------------------------------------------------------------------------
# Shard
# ---------------------------------------------------------------------------
class ShardAdminForm(forms.ModelForm):
    """alias is chosen ONLY from keys in settings.DATABASES not yet registered."""

    class Meta:
        model = Shard
        fields = ["alias", "name", "is_default", "is_active"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        used = set(Shard.objects.exclude(pk=self.instance.pk).values_list("alias", flat=True))
        available = [a for a in settings.DATABASES if a not in used]
        # When editing, keep the current alias in the choices.
        if self.instance.pk and self.instance.alias:
            available = sorted(set(available) | {self.instance.alias})
        self.fields["alias"] = forms.ChoiceField(
            choices=[(a, a) for a in available],
            help_text="Database alias declared in settings.DATABASES at startup.",
        )

    def clean_is_default(self) -> bool:
        """At most one default shard. The DB constraint also enforces this."""
        is_default = self.cleaned_data.get("is_default")
        if is_default:
            qs = Shard.objects.filter(is_default=True).exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    f"A default shard already exists: {qs.first().alias}. Only one is allowed."
                )
        return is_default


class ShardAdmin(admin.ModelAdmin):
    form = ShardAdminForm
    list_display = ("alias", "name", "is_default", "is_active", "tenant_count")
    list_filter  = ("is_default", "is_active")

    @admin.display(description="Tenants")
    def tenant_count(self, obj: Shard) -> int:
        return obj.tenants.count()

    def changelist_view(self, request: HttpRequest,
                        extra_context: dict[str, Any] | None = None) -> HttpResponse:
        """Hint admins about aliases in settings.DATABASES that aren't yet registered."""
        registered = set(Shard.objects.values_list("alias", flat=True))
        missing = [a for a in settings.DATABASES if a not in registered]
        if missing:
            self.message_user(
                request,
                f"Unregistered aliases in settings.DATABASES: {', '.join(missing)}. "
                f"Click 'Add Shard' (or run `manage.py sync_shards`).",
                level=messages.INFO,
            )
        return super().changelist_view(request, extra_context=extra_context)

    # Friendly UX for ProtectedError on delete (shard with tenants or default shard).
    def delete_model(self, request: HttpRequest, obj: Shard) -> None:
        try:
            super().delete_model(request, obj)
        except ProtectedError:
            self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet) -> None:
        for obj in queryset:
            try:
                obj.delete()
            except ProtectedError:
                self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    @staticmethod
    def _explain_protected(shard: Shard) -> str:
        if shard.is_default:
            return (f"Shard '{shard.alias}' is the default shard and cannot be deleted. "
                    f"It is reserved for the public schema.")
        tenants = list(shard.tenants.all()[:5])
        if tenants:
            names = ", ".join(t.schema_name for t in tenants)
            count = shard.tenants.count()
            more = f" and {count - 5} more" if count > 5 else ""
            return (f"Shard '{shard.alias}' cannot be deleted - it has {count} tenant(s): "
                    f"{names}{more}. Move or delete tenants first.")
        return f"Shard '{shard.alias}' cannot be deleted (related objects exist)."


# ---------------------------------------------------------------------------
# Tenant
# ---------------------------------------------------------------------------
S = Tenant.Status

# Allowed status transitions in the admin (deploy/migrations_plan_rev7.md §14.2). NEW and
# PENDING are the migrator's; MAINTENANCE is entered and left only here (I7).
STATUS_TRANSITIONS: dict[str, list[str]] = {
    S.NEW:         [S.NEW],
    S.PENDING:     [S.PENDING],
    S.ACTIVE:      [S.ACTIVE, S.DEACTIVATED, S.MAINTENANCE],
    S.DEACTIVATED: [S.DEACTIVATED, S.ACTIVE, S.MAINTENANCE],
    S.FAILED:      [S.FAILED, S.NEW, S.MAINTENANCE],
    S.MAINTENANCE: [S.MAINTENANCE, S.ACTIVE, S.DEACTIVATED, S.FAILED],
}

CONFIRM_SCHEMA_LABEL = ("I checked the schema: its data is restored, or I accept an empty "
                        "schema, or the existing schema belongs to this tenant.")


class StatusConflict(Exception):
    """The tenant's status moved between the operator opening the page and the CAS."""


class TenantAdminForm(forms.ModelForm):
    """Filters shard choices based on public/non-public and restricts status transitions.

    Status changes are validated here (clean) and written by TenantAdmin.save_model as a
    CAS. → ACTIVE goes through the reactivation stopper; → MAINTENANCE is refused while a
    migrator holds the tenant's lease; FAILED → NEW needs `confirm_schema` (the migrator
    then accepts the existing schema: previous_status = failed, M4).

    The base of a status change is `expected_status`: the status the operator SAW, carried
    in a hidden field from the GET. The admin re-reads the row on POST, so `instance.status`
    is already the fresh value — basing the change on it would let a stale page silently
    overwrite a concurrent change (a console deactivation, the migrator's FAILED)."""

    confirm_schema = forms.BooleanField(required=False, label=CONFIRM_SCHEMA_LABEL)
    expected_status = forms.CharField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Tenant
        fields = "__all__"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance") or self.instance
        public = get_public_schema_name()
        self.old_status = None
        if instance and instance.pk:
            posted = self.data.get(self.add_prefix("expected_status")) if self.is_bound else None
            self.old_status = posted if posted in Tenant.Status.values else instance.status
            self.fields["expected_status"].initial = self.old_status
            self.initial["status"] = self.old_status    # a disabled field keeps what was shown

        # Shard choices: public tenant -> default only; others -> non-default active only.
        if instance and instance.pk and instance.schema_name == public:
            self.fields["shard"].queryset = Shard.objects.filter(is_default=True)
        else:
            self.fields["shard"].queryset = Shard.objects.filter(
                is_default=False, is_active=True
            )

        if instance and instance.pk:
            current = self.old_status
            allowed = STATUS_TRANSITIONS.get(current, [current])
            self.fields["status"].choices = [
                (s, dict(Tenant.Status.choices)[s]) for s in allowed
            ]
            if current in (Tenant.Status.PENDING, Tenant.Status.NEW):
                self.fields["status"].disabled = True
            if current == Tenant.Status.FAILED:
                self.fields["confirm_schema"].help_text = (
                    f"Needed for FAILED → New. Schema now: {self._schema_state(instance)}.")
        else:
            self.fields["status"].initial = Tenant.Status.NEW
            self.fields["status"].choices = [(Tenant.Status.NEW, "New")]

    @staticmethod
    def _schema_state(t: Tenant) -> str:
        try:
            n = migrator.schema_table_count(t.shard.alias, t.schema_name)
        except Exception as exc:
            return f"unknown ({t.shard.alias} unreachable: {type(exc).__name__})"
        if n is None:
            return f"does not exist on {t.shard.alias}"
        return f"empty, on {t.shard.alias}" if n == 0 else f"NOT empty ({n} tables) on {t.shard.alias}"

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        old, new = self.old_status, cleaned.get("status")
        if old is None or new is None or new == old:
            return cleaned                  # no status change requested: status is not written
        t = self.instance
        if t.status != old:
            raise forms.ValidationError(
                f"The status changed while this page was open (it is now '{t.status}', "
                f"you saw '{old}'). Nothing was saved; reload the page and try again.")
        if new == Tenant.Status.ACTIVE:
            try:
                migrator.check_reactivation(t)
            except migrator.ReactivationBlocked as exc:
                flag = " --maintenance" if old == Tenant.Status.MAINTENANCE else ""
                hint = ("retry after the deploy / when the shard is back" if exc.status == 503
                        else f"run `migrate_schemas -s {t.schema_name}{flag}` first")
                raise forms.ValidationError({"status": f"Cannot activate: {exc} ({exc.code}); {hint}."})
        elif new == Tenant.Status.MAINTENANCE:
            h = migrator.lease_holder(t.pk)
            if h is not None:
                busy = (not h.expired) or migrator.shard_session_active(t.shard.alias, t.pk)
                if busy:
                    raise forms.ValidationError({"status": (
                        f"Being migrated right now by {h.owner} since "
                        f"{h.since:%Y-%m-%d %H:%M:%S} UTC; wait for it to finish.")})
        if old == Tenant.Status.FAILED and new == Tenant.Status.NEW and not cleaned.get("confirm_schema"):
            raise forms.ValidationError({"confirm_schema": (
                "Tick this to move a FAILED tenant back to New: the next provisioning run "
                "will adopt whatever schema is there.")})
        return cleaned


class TenantAdmin(TenantAdminMixin, admin.ModelAdmin):
    form = TenantAdminForm
    list_display    = ("company_name", "schema_name", "shard", "status", "status_changed_at", "created_on")
    list_filter     = ("status", "shard")
    search_fields   = ("company_name", "schema_name")
    readonly_fields = ("previous_status", "status_changed_at", "last_error", "created_on")

    def get_fields(self, request: HttpRequest, obj: Tenant | None = None) -> list[str]:
        """confirm_schema only on a FAILED tenant (the only status where it means anything);
        expected_status only on the change form."""
        fields = list(super().get_fields(request, obj))
        if obj is None or obj.status != Tenant.Status.FAILED:
            fields = [f for f in fields if f != "confirm_schema"]
        if obj is None:
            fields = [f for f in fields if f != "expected_status"]
        return fields

    def get_readonly_fields(self, request: HttpRequest,
                            obj: Tenant | None = None) -> list[str]:
        """schema_name maps to a physical PG schema — immutable once created. Read-only
        on the change form; still settable on the add form."""
        ro = list(super().get_readonly_fields(request, obj))
        if obj is not None and "schema_name" not in ro:
            ro.append("schema_name")
        return ro

    def save_model(self, request: HttpRequest, obj: Tenant, form: TenantAdminForm,
                   change: bool) -> None:
        """A status change is a CAS on the status the operator saw (§14.2; the form's
        `expected_status`). Other fields are saved with update_fields — never `status` — so
        an edit of company_name cannot resurrect a stale status either. The form already
        refuses a status that moved before the POST; a move in the milliseconds between
        clean() and this CAS raises StatusConflict, which leaves the admin's atomic() — so
        save_related / log_change never run on a doomed transaction — and is turned into a
        message by changeform_view."""
        if not change:
            super().save_model(request, obj, form, change)
            return
        old, new = form.old_status, obj.status
        other = [f for f in form.changed_data
                 if f not in ("status", "confirm_schema", "expected_status")]
        if new != old:
            if new == Tenant.Status.MAINTENANCE:
                # The form's lease check and this write are separate steps: lock the lease
                # row for the rest of this transaction so no migrator can start between them.
                h = migrator.lease_lock_for_transition(obj.pk)
                if h is not None:
                    raise StatusConflict(
                        f"'{obj.schema_name}' started migrating meanwhile ({h.owner} since "
                        f"{h.since:%Y-%m-%d %H:%M:%S} UTC); nothing was saved. Wait for it to "
                        f"finish and try again.")
            fields: dict[str, Any] = {"status": new, "previous_status": old,
                                      "status_changed_at": timezone.now()}
            if new == Tenant.Status.NEW:
                fields["last_error"] = ""
            if not Tenant.objects.filter(pk=obj.pk, status=old).update(**fields):
                raise StatusConflict(
                    f"The status of '{obj.schema_name}' changed concurrently (it is no longer "
                    f"'{old}'); nothing was saved. Reload the page and try again.")
            # .update() skips post_save, so invalidate the resolve snapshot — after commit,
            # or a rollback would leave a forgotten entry refilled from the OLD row.
            transaction.on_commit(lambda: resolve_cache.forget_tenant(obj))
            obj.previous_status = old
            obj._loaded_status = new            # Tenant.save() must not re-stamp the change
        else:
            # No status change requested. obj.status holds the value the PAGE showed; if
            # someone moved the status since, Tenant.save() would see a "change" and stamp
            # status_changed_at over theirs. Keep the row's own status on the object.
            obj.status = getattr(obj, "_loaded_status", obj.status)
        if other:
            obj.save(update_fields=other)       # post_save → forget_tenant, as before

    def changeform_view(self, request: HttpRequest, object_id: str | None = None,
                        form_url: str = "", extra_context: dict[str, Any] | None = None
                        ) -> HttpResponse:
        # Outside the admin's atomic(): by the time StatusConflict gets here the
        # transaction has already been rolled back by the exception itself.
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except StatusConflict as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return HttpResponseRedirect(request.path)

    def delete_model(self, request: HttpRequest, obj: Tenant) -> None:
        try:
            super().delete_model(request, obj)
        except ProtectedError:
            self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet) -> None:
        for obj in queryset:
            try:
                obj.delete()
            except ProtectedError:
                self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    @staticmethod
    def _explain_protected(tenant: Tenant) -> str:
        if tenant.schema_name == get_public_schema_name():
            return ("The public tenant cannot be deleted - it is a system record "
                    "required for django-tenants to operate on the public host.")
        return f"Tenant '{tenant.schema_name}' cannot be deleted (related objects exist)."


class DomainAdmin(admin.ModelAdmin):
    list_display  = ("domain", "tenant", "is_primary")
    search_fields = ("domain",)


# ---------------------------------------------------------------------------
# ReservedHostRule
# ---------------------------------------------------------------------------
class ReservedHostRuleAdmin(admin.ModelAdmin):
    """Manage reserved-host rules. Model.clean() normalizes/validates value and
    base_domain per match_type (the ModelForm runs full_clean), so the admin and
    the management API enforce identical rules.
    """

    list_display  = ("__str__", "match_type", "value", "base_domain", "is_active", "modified")
    list_filter   = ("match_type", "is_active")
    search_fields = ("value", "base_domain", "note")
    readonly_fields = ("created_on", "modified")
    fields = ("match_type", "value", "base_domain", "is_active", "note",
              "created_on", "modified")


# ---------------------------------------------------------------------------
# Registration on the public admin site
# ---------------------------------------------------------------------------
public_admin_site.register(Shard, ShardAdmin)
public_admin_site.register(Tenant, TenantAdmin)
public_admin_site.register(Domain, DomainAdmin)
public_admin_site.register(ReservedHostRule, ReservedHostRuleAdmin)
public_admin_site.register(Group, GroupAdmin)
