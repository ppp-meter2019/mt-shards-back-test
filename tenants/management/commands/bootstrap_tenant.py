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

from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context

from tenants.models import Domain, Tenant
from users.models import User


class Command(BaseCommand):
    help = "Create a tenant + its primary domain + a company-admin user."

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--domain", required=True)
        parser.add_argument("--admin-username", default="admin")
        parser.add_argument("--admin-password", default="adminpass")
        parser.add_argument("--admin-email", default="admin@example.com")

    def handle(self, *args, **opts):
        schema = opts["schema"].lower()
        if schema == "public":
            self.stderr.write("Refusing to overwrite 'public' — use bootstrap_public.")
            return

        tenant, created = Tenant.objects.get_or_create(
            schema_name=schema,
            defaults={"name": opts["name"]},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Tenant '{schema}' created."))

        Domain.objects.get_or_create(
            domain=opts["domain"],
            defaults={"tenant": tenant, "is_primary": True},
        )

        # Switch search_path to the tenant schema so we create the user in the
        # right auth_user table.
        with schema_context(schema):
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