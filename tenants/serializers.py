import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import Error as DBError, connections, transaction
from django.db.utils import ConnectionDoesNotExist
from django_tenants.utils import get_public_schema_name
from rest_framework import serializers

from users.models import User

from .context import tenant_context
from .models import Domain, ReservedHostRule, Shard, Tenant
from .resolver import resolve_cache
from .validators import validate_tenant_domain, validate_tenant_schema_name

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
    """Read-only representation of a Shard.

    Used in two places:
      - the tenant create-form dropdown / tenant rows (nested in TenantSerializer);
      - the Shards management page (ShardViewSet), which also needs tenant_count
        and timestamps.
    """

    # Annotated by ShardViewSet (Count("tenants")) for the management list.
    # Absent when nested in TenantSerializer (the tenant list doesn't need it)
    # -> None, so we don't trigger an extra query per nested shard.
    tenant_count = serializers.SerializerMethodField()

    class Meta:
        model = Shard
        fields = [
            "id", "alias", "name", "is_default", "is_active",
            "tenant_count", "created_on", "modified",
        ]
        read_only_fields = fields

    def get_tenant_count(self, obj):
        return getattr(obj, "tenant_count", None)


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

    # required on create; made optional on update in __init__ (omit => keep current
    # primary domain, provide => repoint it).
    domain = serializers.CharField(write_only=True, required=True)
    domains = DomainSerializer(many=True, read_only=True)
    admins = serializers.SerializerMethodField()

    # Optional notes. The model TextField is unbounded; cap the API input here so a
    # request can't carry an oversized blob.
    description = serializers.CharField(max_length=300, required=False, allow_blank=True)

    # Pre-computed in TenantViewSet.get_serializer_context() with one query
    # per shard (see _existing_schemas_for there) — avoids N+1.
    schema_exists = serializers.SerializerMethodField()

    # True for the public/management tenant. It is listed but read-only (the
    # API rejects every write); the UI uses this to hide its action buttons.
    is_public = serializers.SerializerMethodField()

    # Last applied migration in the tenant's own schema. Pre-computed per shard
    # in TenantViewSet.get_serializer_context() (see _last_migrations_for).
    last_migration = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            "id",
            "schema_name",
            "company_name",
            "description",
            "shard",
            "shard_id",
            "status",
            "status_changed_at",
            "last_error",
            "schema_exists",
            "is_public",
            "last_migration",
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
            "schema_exists", "is_public", "last_migration",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # update-write ONLY: instance present AND bound to input data (DRF sets
        # initial_data only when data= was passed). This excludes retrieve/list
        # serialization (instance present, no data) and the many= child (whose
        # instance is the queryset) — for which the toggle would be a no-op on read
        # but semantically wrong.
        is_update = self.instance is not None and hasattr(self, "initial_data")
        if is_update:
            # schema_name maps to a physical PG schema; shard move is a separate
            # (dangerous) operation we deliberately don't expose here. Both become
            # read-only on update; domain becomes optional (omit => keep current).
            self.fields["schema_name"].read_only = True
            self.fields["shard_id"].read_only = True
            self.fields["domain"].required = False

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
        except (DBError, ConnectionDoesNotExist):
            # Tenant schema broken / not yet migrated / shard unreachable → don't blow
            # up the listing, just show no admins. Narrowed to DB/connection errors so
            # a real programming error still surfaces (500) instead of hiding here.
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

    def get_is_public(self, obj: Tenant) -> bool:
        """True for the public/management tenant (listed but read-only)."""
        return obj.schema_name == get_public_schema_name()

    def get_last_migration(self, obj: Tenant):
        """{"app","name","applied"} of the latest migration in the tenant's
        schema, or None. Read from context["last_migrations"] (pre-computed per
        shard in the viewset); None when used outside the viewset.
        """
        table = self.context.get("last_migrations")
        if table is None:
            return None
        return table.get((obj.shard.alias, obj.schema_name))

    def validate_schema_name(self, value: str) -> str:
        value = value.strip().lower()
        if value == get_public_schema_name():
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
        # Reject reserved global service labels (www/api/admin/...) — same source
        # as Domain validation (tenants.ReservedHostRule).
        try:
            validate_tenant_schema_name(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        if Tenant.objects.filter(schema_name=value).exists():
            raise serializers.ValidationError(
                f"Tenant with schema '{value}' already exists."
            )
        return value

    def validate_domain(self, value: str) -> str:
        # Format + code invariants (valid hostname, platform hosts) + operator-managed
        # reserved-host rules (tenants.ReservedHostRule). Raises a django
        # ValidationError which we surface as a friendly DRF 400.
        try:
            value = validate_tenant_domain(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        # Catch duplicate domains here so the caller gets a friendly 400 with the
        # offending value, not a 500 IntegrityError from the DB layer. On update we
        # exclude ONLY the primary row we're about to repoint — re-submitting the
        # current primary is a no-op, but any OTHER existing row (incl. this tenant's
        # OWN secondary domain) is a real UNIQUE collision.
        clash = Domain.objects.filter(domain=value)
        if self.instance is not None:
            primary = self.instance.domains.filter(is_primary=True).first()
            if primary is not None:
                clash = clash.exclude(pk=primary.pk)
        if clash.exists():
            raise serializers.ValidationError(f"Domain '{value}' is already in use.")
        return value

    def validate(self, attrs):
        """On create, reject a schema that PHYSICALLY exists on the chosen shard.

        validate_schema_name only checks the Tenant table; an orphan schema left
        by a deleted tenant (auto_drop_schema=False) has no Tenant row, so it
        would slip through — and migrate_schemas would then reuse it and inherit
        stale data. Here we have both schema_name and the shard, so we can check
        the real schema and refuse.
        """
        attrs = super().validate(attrs)
        if self.instance is None:                       # create only
            schema = attrs.get("schema_name")
            shard = attrs.get("shard")                  # source of write-only shard_id
            if schema and shard:
                with connections[shard.alias].cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                        [schema],
                    )
                    if cur.fetchone() is not None:
                        raise serializers.ValidationError({
                            "schema_name": (
                                f"Schema '{schema}' already exists on shard "
                                f"'{shard.alias}' (orphaned from a deleted tenant?). "
                                f"Drop it first (manage.py drop_tenant_schema) or "
                                f"choose another name."
                            )
                        })
        return attrs

    def create(self, validated_data):
        domain = validated_data.pop("domain")
        with transaction.atomic():
            tenant = Tenant.objects.create(**validated_data)
            Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
        return tenant

    def update(self, instance, validated_data):
        """Update company_name/description and, if `domain` is given, repoint the
        tenant's PRIMARY domain. schema_name/shard are read-only here (see __init__).
        """
        domain = validated_data.pop("domain", None)
        with transaction.atomic():
            tenant = super().update(instance, validated_data)
            if domain:
                primary = instance.domains.filter(is_primary=True).first()
                old_host = primary.domain if primary else None
                if old_host != domain:
                    if primary is not None:
                        primary.domain = domain
                        primary.save()          # post_save signal invalidates the NEW host
                    else:
                        Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
                    if old_host:
                        # The signal only knows the NEW value; drop the OLD host too,
                        # else it keeps resolving to this tenant until its TTL expires.
                        resolve_cache.forget_host(old_host)
        return tenant


class ReservedHostRuleSerializer(serializers.ModelSerializer):
    """CRUD for the operator-managed reserved-host rules (management UI + admin API).

    Normalizes and validates `value`/`base_domain` per `match_type` using the same
    validators as Domain.clean(), so admin and API behave identically.
    """

    class Meta:
        model = ReservedHostRule
        fields = [
            "id", "match_type", "value", "base_domain",
            "is_active", "note", "created_on", "modified",
        ]
        read_only_fields = ["id", "created_on", "modified"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Support PATCH: fall back to the instance's current values.
        match_type = attrs.get(
            "match_type", getattr(self.instance, "match_type", None)
        )
        value = attrs.get("value", getattr(self.instance, "value", ""))
        base_domain = attrs.get("base_domain", getattr(self.instance, "base_domain", ""))

        try:
            attrs["value"], attrs["base_domain"] = ReservedHostRule.normalize(
                match_type, value, base_domain)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"value": exc.messages})

        # Friendly duplicate check against the (match_type, value, base_domain) unique
        # constraint, so the caller gets a 400 instead of a 500 IntegrityError.
        dupe = ReservedHostRule.objects.filter(
            match_type=match_type,
            value=attrs["value"],
            base_domain=attrs.get("base_domain", ""),
        )
        if self.instance is not None:
            dupe = dupe.exclude(pk=self.instance.pk)
        if dupe.exists():
            raise serializers.ValidationError("An identical rule already exists.")
        return attrs
