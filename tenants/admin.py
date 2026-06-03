"""Two AdminSites coexist:

- `public_admin_site` - mounted only on the public host (urls_public.py).
  Tenant administrators manage shards, tenants, domains and platform users.
- `admin.site` (default) - mounted on each tenant host (urls_tenant.py).
  Company administrators manage business models (cars, orders, ...).

Models that exist in both schemas (User) are registered on BOTH sites;
models that only exist in one schema are registered only where they live.
"""

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import AdminSite
from django.contrib.auth.admin import GroupAdmin
from django.contrib.auth.models import Group
from django.db.models.deletion import ProtectedError
from django_tenants.admin import TenantAdminMixin
from django_tenants.utils import get_public_schema_name

from .models import Domain, Shard, Tenant


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

    def __init__(self, *args, **kwargs):
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

    def clean_is_default(self):
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
    def tenant_count(self, obj):
        return obj.tenants.count()

    def changelist_view(self, request, extra_context=None):
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
    def delete_model(self, request, obj):
        try:
            super().delete_model(request, obj)
        except ProtectedError:
            self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            try:
                obj.delete()
            except ProtectedError:
                self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    @staticmethod
    def _explain_protected(shard):
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
class TenantAdminForm(forms.ModelForm):
    """Filters shard choices based on public/non-public and restricts status transitions."""

    class Meta:
        model = Tenant
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance") or self.instance
        public = get_public_schema_name()

        # Shard choices: public tenant -> default only; others -> non-default active only.
        if instance and instance.pk and instance.schema_name == public:
            self.fields["shard"].queryset = Shard.objects.filter(is_default=True)
        else:
            self.fields["shard"].queryset = Shard.objects.filter(
                is_default=False, is_active=True
            )

        # Allowed status transitions (admin can deactivate/reactivate; everything
        # else is system-managed).
        if instance and instance.pk:
            current = instance.status
            allowed = {
                Tenant.Status.NEW:         [Tenant.Status.NEW],
                Tenant.Status.PENDING:     [Tenant.Status.PENDING],
                Tenant.Status.ACTIVE:      [Tenant.Status.ACTIVE, Tenant.Status.DEACTIVATED],
                Tenant.Status.DEACTIVATED: [Tenant.Status.ACTIVE, Tenant.Status.DEACTIVATED],
                Tenant.Status.FAILED:      [Tenant.Status.FAILED, Tenant.Status.NEW],
            }.get(current, [current])
            self.fields["status"].choices = [
                (s, dict(Tenant.Status.choices)[s]) for s in allowed
            ]
            if current in (Tenant.Status.PENDING, Tenant.Status.NEW):
                self.fields["status"].disabled = True
        else:
            self.fields["status"].initial = Tenant.Status.NEW
            self.fields["status"].choices = [(Tenant.Status.NEW, "New")]


class TenantAdmin(TenantAdminMixin, admin.ModelAdmin):
    form = TenantAdminForm
    list_display    = ("name", "schema_name", "shard", "status", "status_changed_at", "created_on")
    list_filter     = ("status", "shard")
    search_fields   = ("name", "schema_name")
    readonly_fields = ("previous_status", "status_changed_at", "last_error", "created_on")

    def delete_model(self, request, obj):
        try:
            super().delete_model(request, obj)
        except ProtectedError:
            self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            try:
                obj.delete()
            except ProtectedError:
                self.message_user(request, self._explain_protected(obj), level=messages.ERROR)

    @staticmethod
    def _explain_protected(tenant):
        if tenant.schema_name == get_public_schema_name():
            return ("The public tenant cannot be deleted - it is a system record "
                    "required for django-tenants to operate on the public host.")
        return f"Tenant '{tenant.schema_name}' cannot be deleted (related objects exist)."


class DomainAdmin(admin.ModelAdmin):
    list_display  = ("domain", "tenant", "is_primary")
    search_fields = ("domain",)


# ---------------------------------------------------------------------------
# Registration on the public admin site
# ---------------------------------------------------------------------------
public_admin_site.register(Shard, ShardAdmin)
public_admin_site.register(Tenant, TenantAdmin)
public_admin_site.register(Domain, DomainAdmin)
public_admin_site.register(Group, GroupAdmin)
