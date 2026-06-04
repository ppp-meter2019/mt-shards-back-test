import re

from django.db import transaction
from rest_framework import serializers

from users.models import User

from .context import tenant_context
from .models import Domain, Shard, Tenant

# ASCII PostgreSQL-safe schema name: starts with a lowercase letter, then
# lowercase letters / digits / underscores, total 1-63 chars. ASCII-only on
# purpose - `str.isalnum()` would accept Unicode letters/digits (e.g. Cyrillic
# or non-ASCII digits), which we do not want in a schema identifier.
_SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class DomainSerializer(serializers.ModelSerializer):
    class Meta:
        model = Domain
        fields = ["id", "domain", "is_primary"]


class ShardSerializer(serializers.ModelSerializer):
    """Read-only representation of a Shard for tenant CRUD.

    Exposes only the identity (id/alias/name) plus is_default/is_active so the
    UI can populate the shard dropdown and show shard info on tenant rows.
    """

    class Meta:
        model = Shard
        fields = ["id", "alias", "name", "is_default", "is_active"]
        read_only_fields = fields


class TenantSerializer(serializers.ModelSerializer):
    """Lets a tenant-admin create/list tenants from the public host.

    Read side returns the shard nested (so the UI can show alias/name) plus
    a `schema_exists` flag that the UI uses to decide which action buttons
    to render. Write side accepts `shard_id` and a primary `domain`.
    """

    # On reads: full nested shard. On writes: just shard_id (FK).
    shard = ShardSerializer(read_only=True)
    shard_id = serializers.PrimaryKeyRelatedField(
        source="shard",
        write_only=True,
        queryset=Shard.objects.filter(is_active=True, is_default=False),
    )

    domain = serializers.CharField(write_only=True, required=True)
    domains = DomainSerializer(many=True, read_only=True)
    admins = serializers.SerializerMethodField()

    # Pre-computed in TenantViewSet.get_serializer_context() with one query
    # per shard (see _existing_schemas_for there) — avoids N+1.
    schema_exists = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            "id",
            "schema_name",
            "name",
            "shard",
            "shard_id",
            "status",
            "status_changed_at",
            "last_error",
            "schema_exists",
            "created_on",
            "domain",
            "domains",
            "admins",
        ]
        # status is managed by migrate_schemas / reconcile_tenants commands and
        # by dedicated actions (activate/deactivate); the API never lets
        # clients write it via the generic serializer.
        read_only_fields = [
            "id", "created_on", "domains", "admins",
            "status", "status_changed_at", "last_error",
            "schema_exists",
        ]

    def get_admins(self, obj: Tenant) -> list:
        """List of company-admin usernames inside the tenant's schema.

        One extra query per tenant, executed on the tenant's SHARD inside its
        schema via `tenant_context` (wires both the router alias and the
        search_path). Cheap for a tens-of-tenants admin UI; if you ever get
        hundreds of tenants, replace with a single cross-schema raw SQL query.
        """
        try:
            with tenant_context(obj):
                return list(
                    User.objects.filter(role=User.Role.COMPANY_ADMIN)
                    .order_by("username")
                    .values("id", "username", "is_active")
                )
        except Exception:
            # If the tenant schema is broken / not yet migrated, don't blow up
            # the whole listing — just return an empty list of admins.
            return []

    def get_schema_exists(self, obj: Tenant) -> bool:
        """Whether the tenant's schema actually exists in its shard database.

        Reads from `context["existing_schemas"]`, which the viewset pre-fills
        with a single SELECT against information_schema.schemata per shard.
        Falls back to False if not provided (e.g., serializer used outside
        the viewset).
        """
        existing = self.context.get("existing_schemas")
        if existing is None:
            return False
        return (obj.shard.alias, obj.schema_name) in existing

    def validate_schema_name(self, value: str) -> str:
        value = value.strip().lower()
        if value == "public":
            raise serializers.ValidationError("Schema name 'public' is reserved.")
        if not _SCHEMA_NAME_RE.fullmatch(value):
            raise serializers.ValidationError(
                "schema_name must be ASCII: start with a lowercase letter, then "
                "lowercase letters, digits or underscores (max 63 chars)."
            )
        if value.startswith("pg_"):
            raise serializers.ValidationError(
                "schema_name cannot start with 'pg_' (reserved by PostgreSQL)."
            )
        if Tenant.objects.filter(schema_name=value).exists():
            raise serializers.ValidationError(
                f"Tenant with schema '{value}' already exists."
            )
        return value

    def validate_domain(self, value: str) -> str:
        # Catch duplicate domains here so the caller gets a friendly 400 with
        # the offending value, not a 500 IntegrityError from the DB layer.
        value = value.strip().lower()
        if Domain.objects.filter(domain=value).exists():
            raise serializers.ValidationError(
                f"Domain '{value}' is already in use by another tenant."
            )
        return value

    def create(self, validated_data):
        domain = validated_data.pop("domain")
        with transaction.atomic():
            tenant = Tenant.objects.create(**validated_data)
            Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
        return tenant
