"""
Create a tenant (schema, domain) and seed it with a company-admin user.

Example:
    python manage.py bootstrap_tenant \
        --schema alpha \
        --name "Alpha LLC" \
        --domain alpha.localhost \
        --admin-username admin \
        --admin-password adminpass
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from tenants.context import tenant_context
from tenants.models import Domain, Shard, Tenant
from users.models import User


class Command(BaseCommand):
    help = "Create a tenant + its primary domain + a company-admin user."

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--domain", required=True)
        parser.add_argument(
            "--shard", required=True,
            help="Non-default, active Shard alias to place this tenant on.",
        )
        parser.add_argument("--admin-username", default="admin")
        parser.add_argument("--admin-password", default="adminpass")
        parser.add_argument("--admin-email", default="admin@example.com")

    def handle(self, *args, **opts):
        schema = opts["schema"].lower()
        if schema == "public":
            raise CommandError("Refusing to overwrite 'public' — use bootstrap_public.")

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
            defaults={"name": opts["name"], "shard": shard, "status": Tenant.Status.NEW},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Tenant '{schema}' created (status=NEW)."))

        Domain.objects.get_or_create(
            domain=opts["domain"],
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