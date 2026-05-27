"""
Idempotently create the `public` tenant + its primary domain + a
tenant-administrator user. Run once after `migrate_schemas --shared`.

Example:
    python manage.py bootstrap_public \
        --domain localhost \
        --username root \
        --password rootpass
"""

from django.core.management.base import BaseCommand

from tenants.models import Domain, Tenant
from users.models import User


class Command(BaseCommand):
    help = "Create the public tenant + a tenant-admin user."

    def add_arguments(self, parser):
        parser.add_argument("--domain", default="localhost")
        parser.add_argument("--username", default="root")
        parser.add_argument("--password", default="rootpass")
        parser.add_argument("--email", default="root@example.com")

    def handle(self, *args, **opts):
        tenant, created = Tenant.objects.get_or_create(
            schema_name="public",
            defaults={"name": "Public"},
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Created public tenant."))
        Domain.objects.get_or_create(
            domain=opts["domain"],
            defaults={"tenant": tenant, "is_primary": True},
        )

        user, created = User.objects.get_or_create(
            username=opts["username"],
            defaults={
                "email": opts["email"],
                "role": User.Role.TENANT_ADMIN,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        user.role = User.Role.TENANT_ADMIN
        user.is_staff = True
        user.is_superuser = True
        user.set_password(opts["password"])
        user.save()
        self.stdout.write(
            self.style.SUCCESS(
                f"Tenant-admin '{user.username}' ready on host '{opts['domain']}'."
            )
        )