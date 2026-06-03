"""Multi-DB-aware migrate_schemas. Overrides the django-tenants version.

For this override to take effect, `tenants` must be listed BEFORE `django_tenants`
in SHARED_APPS (so that Django's command resolution picks our class).

Behavior (extends upstream):

  --shared
      SHARED_APPS migrations on default.public. --database other than 'default'
      is ignored with a warning.

  --schema_name=acme
      Migrate only the schema 'acme'. The target shard is resolved from
      Tenant.shard.alias. If --database is also passed and doesn't match,
      a CommandError is raised. For tenants in status=NEW with no schema,
      the schema is created automatically as part of the run.

  --tenant --database=tenant_1
      All tenants whose Tenant.shard.alias == 'tenant_1'.

  --tenant            (or no flags at all + status-aware iteration)
      Iterate every non-default Shard separately, migrating its tenants.

  (no arguments)
      Both --shared and --tenant (full system migration).

Status state machine for tenant migrations:
  - Tenants are claimable when status in (NEW, ACTIVE, DEACTIVATED).
  - PENDING and FAILED tenants are skipped (admin attention required).
  - Atomic claim: UPDATE ... WHERE status IN (...) -> PENDING, previous_status=F(status).
    Postgres serializes concurrent claims via row-level locking.
  - On success: PENDING -> previous_status (or ACTIVE if previous was NEW).
  - On failure: PENDING -> FAILED, last_error populated, exception re-raised.

Executor flags (--fake, --plan, --list, --check, --prune, --run-syncdb,
--fake-initial, app_label, migration_name) are forwarded to the executor.
"""

from django.core.management.base import CommandError
from django.db import connections
from django.db.models import F
from django.utils import timezone

from django_tenants.management.commands import SyncCommon
from django_tenants.management.commands.migrate_schemas import (
    MigrateSchemasCommand as UpstreamCommand,
    GET_EXECUTOR_FUNCTION,
)
from django_tenants.utils import (
    get_multi_type_database_field_name,
    get_public_schema_name,
    get_tenant_migration_order,
    has_multi_type_tenants,
    schema_exists,
)

from tenants.models import Shard, Tenant


# Statuses that may be claimed by a migration run.
CLAIMABLE_STATUSES = [
    Tenant.Status.NEW,
    Tenant.Status.ACTIVE,
    Tenant.Status.DEACTIVATED,
]


class Command(UpstreamCommand):
    help = (
        "Multi-DB-aware migrate_schemas. Iterates tenants by Shard.alias, "
        "with atomic status claim. See module docstring for examples."
    )

    # ------------------------------------------------------------------
    # CLI
    # ------------------------------------------------------------------
    def add_arguments(self, parser):
        super().add_arguments(parser)
        # Strip the upstream default of --database='default' so we can
        # distinguish "not specified" from "explicitly default".
        for action in parser._actions:
            if action.dest == "database":
                action.default = None
                action.help = (
                    "Database alias. Multi-DB: filters tenants by Shard.alias. "
                    "Omit to process all shards (in --tenant mode)."
                )
                break

    # ------------------------------------------------------------------
    # Main flow
    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        # SyncCommon parses --shared/--tenant/--schema_name/--executor and sets
        # self.sync_public, self.sync_tenant, self.schema_name, self.executor.
        SyncCommon.handle(self, *args, **options)
        self.PUBLIC_SCHEMA_NAME = get_public_schema_name()

        # schema_name=public is equivalent to --shared.
        if self.schema_name == self.PUBLIC_SCHEMA_NAME:
            self.schema_name = None
            self.sync_public = True
            self.sync_tenant = False

        # schema_name=<specific tenant>: resolve shard and validate consistency.
        resolved_tenant = None
        if self.schema_name:
            try:
                resolved_tenant = (
                    Tenant.objects.select_related("shard").get(schema_name=self.schema_name)
                )
            except Tenant.DoesNotExist:
                raise CommandError(f"Tenant {self.schema_name!r} not found.")
            expected_db = resolved_tenant.shard.alias
            given_db = self.options.get("database")
            if given_db and given_db != expected_db:
                raise CommandError(
                    f"--database={given_db!r} does not match tenant "
                    f"{self.schema_name!r} (its shard is {expected_db!r})."
                )
            # Guard: business tenants must not live on the default shard.
            if resolved_tenant.shard.is_default:
                raise CommandError(
                    f"Tenant {self.schema_name!r} has shard='{expected_db}' "
                    f"(is_default=True). Tenant migrations on the default database "
                    f"are not allowed. Inspect Tenant.shard - this looks like a "
                    f"clean()-bypass anomaly."
                )
            self.options["database"] = expected_db

        # SHARED migrations always go to default.
        if self.sync_public:
            given_db = self.options.get("database")
            if given_db and given_db != "default":
                self._notice(
                    f"--database={given_db!r} ignored for --shared mode; using 'default'."
                )
            self.options["database"] = "default"
            self._notice("=== SHARED_APPS migrations on default.public ===")
            self._get_executor().run_migrations(tenants=[self.PUBLIC_SCHEMA_NAME])

        # TENANT migrations.
        if self.sync_tenant:
            if self.schema_name:
                # Single tenant - shard already validated above.
                self._migrate_one(resolved_tenant)
            else:
                given_db = self.options.get("database")
                if given_db == "default":
                    self._notice(
                        "Database 'default' has no tenant schemas - nothing to do."
                    )
                elif given_db:
                    self._migrate_all_on_shard(given_db)
                else:
                    shards = list(
                        Shard.objects.filter(is_default=False, is_active=True)
                                     .values_list("alias", flat=True)
                    )
                    if not shards:
                        self._notice("No active non-default shards found.")
                    for shard_alias in shards:
                        self._migrate_all_on_shard(shard_alias)

    # ------------------------------------------------------------------
    def _migrate_all_on_shard(self, db_alias):
        """Iterate claimable tenants on a specific shard and migrate each."""
        # Defense in depth: tenant migrations must never target 'default'.
        if db_alias == "default":
            self._notice(
                "Skipping tenant migrations on 'default' - this DB hosts only the public schema."
            )
            return

        qs = (
            Tenant.objects.select_related("shard")
                          .filter(shard__alias=db_alias)
                          .exclude(schema_name=self.PUBLIC_SCHEMA_NAME)
                          .filter(status__in=CLAIMABLE_STATUSES)
        )
        migration_order = get_tenant_migration_order()
        if migration_order is not None:
            qs = qs.order_by(*migration_order)

        tenants = list(qs)
        if not tenants:
            self._notice(f"No claimable tenants on shard {db_alias!r} - skipping.")
            return

        self._notice(f"=== Shard '{db_alias}': {len(tenants)} tenant(s) eligible ===")
        for t in tenants:
            self._migrate_one(t)

    # ------------------------------------------------------------------
    def _migrate_one(self, tenant):
        """Atomic claim -> ensure schema exists -> run migrations -> finalize.

        For tenants whose previous_status was NEW and whose schema does not
        yet exist, the schema is created here as part of provisioning.

        WARNING: do NOT wrap this method (or its callers) in transaction.atomic().
        Django management commands run connections in autocommit mode by default,
        which is required so that:
          1. The CREATE SCHEMA is COMMITTED immediately and visible to the
             subsequent connections opened by the migration executor.
          2. A FAILED status update can be persisted even if executor.run_migrations
             raises later - an outer atomic() would rollback that update along
             with everything else.
        """
        # 1. Atomic claim. Postgres row-level lock serializes concurrent UPDATEs
        #    on the same Tenant row. Only one process moves it into PENDING; the
        #    others see updated_count=0 and skip.
        claimed = Tenant.objects.filter(
            pk=tenant.pk,
            status__in=CLAIMABLE_STATUSES,
        ).update(
            status=Tenant.Status.PENDING,
            previous_status=F("status"),
            status_changed_at=timezone.now(),
            last_error="",
        )
        if claimed == 0:
            self._notice(f"SKIP {tenant.schema_name} - already claimed by another process.")
            return

        # Re-read so we have the updated previous_status.
        tenant.refresh_from_db()
        conn = connections[tenant.shard.alias]

        # 2. Ensure schema exists.
        #    In autocommit mode (default for management commands), CREATE SCHEMA
        #    is committed immediately and is visible to any connection that the
        #    executor opens for the actual migrations.
        if not schema_exists(tenant.schema_name, conn):
            if tenant.previous_status == Tenant.Status.NEW:
                with conn.cursor() as cur:
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{tenant.schema_name}"')
                self._notice(
                    f"  -> Created schema {tenant.schema_name!r} on {tenant.shard.alias!r}"
                )
            else:
                # ACTIVE/DEACTIVATED tenant with no schema -> data loss territory.
                error = (
                    f"Schema {tenant.schema_name!r} missing in shard "
                    f"{tenant.shard.alias!r}, but previous_status={tenant.previous_status!r}. "
                    f"Possible data loss - manual recovery required."
                )
                Tenant.objects.filter(pk=tenant.pk).update(
                    status=Tenant.Status.FAILED,
                    last_error=error,
                    status_changed_at=timezone.now(),
                )
                self._notice(f"FAIL {tenant.schema_name} - {error}")
                raise CommandError(error)

        # 3. Run migrations.
        self.options["database"] = tenant.shard.alias
        executor = self._get_executor()

        try:
            if has_multi_type_tenants():
                type_field = get_multi_type_database_field_name()
                pair = (tenant.schema_name, getattr(tenant, type_field))
                executor.run_multi_type_migrations(tenants=[pair])
            else:
                executor.run_migrations(tenants=[tenant.schema_name])
        except Exception as e:
            # No surrounding atomic() (see method docstring) - this UPDATE persists.
            Tenant.objects.filter(pk=tenant.pk).update(
                status=Tenant.Status.FAILED,
                last_error=f"{type(e).__name__}: {e}"[:2000],
                status_changed_at=timezone.now(),
            )
            self._notice(f"FAIL {tenant.schema_name} - {e}")
            raise

        # 4. Finalize: NEW -> ACTIVE; everything else -> its previous_status.
        new_status = (
            Tenant.Status.ACTIVE
            if tenant.previous_status == Tenant.Status.NEW
            else tenant.previous_status
        )
        Tenant.objects.filter(pk=tenant.pk).update(
            status=new_status,
            status_changed_at=timezone.now(),
        )
        self._notice(f"OK   {tenant.schema_name} -> {new_status}")

    # ------------------------------------------------------------------
    def _get_executor(self):
        # Recreate per shard - options['database'] is mutated between calls.
        return GET_EXECUTOR_FUNCTION(codename=self.executor)(self.args, self.options)
