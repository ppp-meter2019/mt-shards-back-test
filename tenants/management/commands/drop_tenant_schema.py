"""Drop a tenant's PostgreSQL schema on a specific shard (orphan cleanup).

Deleting a Tenant removes only its row + domain (auto_drop_schema=False), so the
schema is left behind on its shard. Since the Tenant row may already be gone,
the shard alias and schema name are passed explicitly.

    python manage.py drop_tenant_schema --database=shard-beta --schema=gamma

Guards:
  * refuses the 'public' schema;
  * refuses a schema still referenced by a LIVE Tenant — delete the tenant
    first (no override: this is strictly an orphan-cleanup tool);
  * validates the alias (must be in DATABASES) and the schema-name format;
  * asks for confirmation (type the schema name) unless --no-input.

Runs DROP SCHEMA "<schema>" CASCADE on the shard's connection (the app role
owns tenant schemas, so it may drop them).
"""
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django_tenants.utils import get_public_schema_name

from tenants.models import Tenant

_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class Command(BaseCommand):
    help = "Drop an orphaned tenant schema on a given shard."

    def add_arguments(self, parser):
        parser.add_argument("--database", required=True, help="Shard alias (DATABASES key).")
        parser.add_argument("--schema", required=True, help="Schema name to drop.")
        parser.add_argument("--no-input", action="store_true",
                            help="Skip the interactive confirmation prompt.")

    def handle(self, *args, **opts):
        alias = opts["database"]
        schema = (opts["schema"] or "").strip().lower()

        if alias not in settings.DATABASES:
            raise CommandError(
                f"Unknown database alias {alias!r}. Available: {sorted(settings.DATABASES)}"
            )
        if schema == get_public_schema_name():
            raise CommandError("Refusing to drop the 'public' schema.")
        if not _SCHEMA_RE.fullmatch(schema):
            raise CommandError(f"Invalid schema name {schema!r}.")

        live = Tenant.objects.filter(schema_name=schema, shard__alias=alias).first()
        if live:
            raise CommandError(
                f"Tenant {schema!r} still exists on shard {alias!r} (status={live.status}). "
                f"Delete the tenant first — this command only cleans up orphaned schemas."
            )

        conn = connections[alias]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [schema]
            )
            if cur.fetchone() is None:
                self.stdout.write(self.style.WARNING(
                    f"Schema {schema!r} not found on shard {alias!r} - nothing to do."
                ))
                return

        if not opts["no_input"]:
            answer = input(
                f"This PERMANENTLY drops schema '{schema}' and ALL its data on shard "
                f"'{alias}'. Type the schema name to confirm: "
            )
            if answer != schema:
                raise CommandError("Confirmation did not match - aborted.")

        # schema is validated by _SCHEMA_RE above, so safe to interpolate.
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        self.stdout.write(self.style.SUCCESS(f"Dropped schema {schema!r} on shard {alias!r}."))
