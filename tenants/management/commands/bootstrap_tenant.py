"""
Create a tenant (schema, domain) and seed it with a company-admin user.

Example:
    python manage.py bootstrap_tenant \
        --schema alpha \
        --company-name "Alpha LLC" \
        --domain alpha.localhost \
        --admin-username admin \
        --admin-password adminpass
"""

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from tenants.context import tenant_context
from tenants.models import Domain, Shard, Tenant
from tenants.validators import (
    normalize_host,
    validate_tenant_domain,
    validate_tenant_schema_name,
)
from users.models import User


class Command(BaseCommand):
    help = "Create a tenant + its primary domain + a company-admin user."

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True)
        parser.add_argument(
            "--company-name", "--name", dest="company_name", required=True,
            help="Company name (display label). '--name' is kept as an alias.",
        )
        parser.add_argument("--description", default="", help="Optional free-text notes.")
        parser.add_argument("--domain", required=True)
        parser.add_argument(
            "--shard", required=True,
            help="Non-default, active Shard alias to place this tenant on.",
        )
        parser.add_argument("--admin-username", default="admin")
        parser.add_argument("--admin-password", default="adminpass")
        parser.add_argument("--admin-email", default="admin@example.com")
        parser.add_argument(
            "--force", action="store_true",
            help="Skip reserved-host/schema validation (operator override). Still "
                 "prints what it overrode.",
        )

    def _check_reserved(self, schema, domain, *, force):
        """Enforce the same reserved-host/schema rules as the API/admin.

        Without --force a violation aborts with a CommandError pointing at --force.
        With --force it only WARNS (so the override is visible in the log) and
        proceeds — this is the deliberate CLI escape hatch.
        """
        problems = []
        for check in (lambda: validate_tenant_schema_name(schema),
                      lambda: validate_tenant_domain(domain)):
            try:
                check()
            except ValidationError as exc:
                problems.extend(exc.messages)
        if not problems:
            return
        joined = "; ".join(problems)
        if force:
            self.stdout.write(self.style.WARNING(
                f"--force: overriding reserved-host validation: {joined}"))
        else:
            raise CommandError(f"{joined} Use --force to override.")

    def handle(self, *args, **opts):
        schema = opts["schema"].lower()
        if schema == "public":
            raise CommandError("Refusing to overwrite 'public' — use bootstrap_public.")

        domain = normalize_host(opts["domain"])
        self._check_reserved(schema, domain, force=opts["force"])

        try:
            shard = Shard.objects.get(alias=opts["shard"])
        except Shard.DoesNotExist:
            raise CommandError(
                f"Shard alias {opts['shard']!r} not found. Register it with "
                f"`sync_shards --activate` (and ensure it is in settings.DATABASES)."
            )
        if shard.is_default:
            raise CommandError(
                f"Shard {shard.alias!r} is the default shard; business tenants "
                f"must use a non-default shard."
            )
        if not shard.is_active:
            raise CommandError(f"Shard {shard.alias!r} is not active.")

        tenant, created = Tenant.objects.get_or_create(
            schema_name=schema,
            defaults={
                "company_name": opts["company_name"],
                "description": opts["description"],
                "shard": shard,
                "status": Tenant.Status.NEW,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Tenant '{schema}' created (status=NEW)."))

        Domain.objects.get_or_create(
            domain=domain,
            defaults={"tenant": tenant, "is_primary": True},
        )

        # Provision the schema and run TENANT_APPS migrations on the tenant's
        # shard. migrate_schemas creates the schema for NEW tenants and flips
        # the status NEW -> ACTIVE.
        call_command("migrate_schemas", schema_name=schema)

        # Seed the company-admin INSIDE the tenant schema on its shard.
        # tenant_context wires both axes (router -> shard DB, schema on that
        # shard's connection), so the INSERT lands in <shard>.<schema>.users_user.
        tenant.refresh_from_db()
        with tenant_context(tenant):
            user, _ = User.objects.get_or_create(
                username=opts["admin_username"],
                defaults={
                    "email": opts["admin_email"],
                    "role": User.Role.COMPANY_ADMIN,
                    "is_staff": True,
                    "is_superuser": True,
                },
            )
            user.role = User.Role.COMPANY_ADMIN
            user.is_staff = True
            user.is_superuser = True
            user.set_password(opts["admin_password"])
            user.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Company-admin '{user.username}' ready in schema '{schema}'."
                )
            )