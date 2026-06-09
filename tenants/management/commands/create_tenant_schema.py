"""Explicit CREATE SCHEMA on the tenant's shard (without running migrations).

In the normal workflow you do NOT need this command - migrate_schemas creates
the schema automatically for tenants in status=NEW. Keep this around for DBA
workflows such as restoring a schema from pg_dump before migrating.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django_tenants.utils import get_public_schema_name, schema_exists

from tenants.models import Tenant


class Command(BaseCommand):
    help = "Create the PostgreSQL schema for a tenant in its assigned shard."

    def add_arguments(self, parser):
        parser.add_argument("schema_name")

    def handle(self, *args, **opts):
        schema = opts["schema_name"]
        try:
            tenant = Tenant.objects.select_related("shard").get(schema_name=schema)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant {schema!r} not found in default.")

        # Guard 1: public schema is created automatically by Postgres when the
        # database is created. Don't try to CREATE SCHEMA "public".
        if tenant.schema_name == get_public_schema_name():
            raise CommandError(
                f"The public schema is auto-created by PostgreSQL on CREATE DATABASE. "
                f"Do not run this command for {tenant.schema_name!r}."
            )

        # Guard 2: business tenants must NEVER live on the default shard.
        # Tenant.clean() prevents this normally, but a raw .update() or SQL could
        # bypass clean(). Refuse to act on such inconsistent records.
        if tenant.shard.is_default:
            raise CommandError(
                f"Tenant {schema!r} has shard '{tenant.shard.alias}' (is_default=True). "
                f"Creating tenant schemas on the default database is not allowed. "
                f"Inspect Tenant.shard - this looks like a clean()-bypass anomaly."
            )

        alias = tenant.shard.alias
        conn  = connections[alias]
        if schema_exists(schema, alias):
            self.stdout.write(f"Schema {schema} already exists in {alias}.")
            return

        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
        self.stdout.write(self.style.SUCCESS(f"Created schema {schema} in {alias}."))
