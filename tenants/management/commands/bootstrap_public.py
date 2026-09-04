"""
Idempotently create the `public` tenant + its primary domain + a
tenant-administrator user. Run once after `migrate_schemas --shared`.

Example:
    python manage.py bootstrap_public \
        --domain localhost \
        --username root \
        --password rootpass
"""

from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django_tenants.utils import get_public_schema_name

from commons.platform.commands import TenantCommand

from tenants.context import schema_context
from tenants.models import Domain, Shard, Tenant
from tenants.validators import validate_hostname
from users.models import User


class Command(TenantCommand):
    help = "Create the public tenant + a tenant-admin user."

    def add_arguments(self, parser):
        parser.add_argument("--domain", default="localhost")
        parser.add_argument("--username", default="root")
        parser.add_argument("--password", default="rootpass")
        parser.add_argument("--email", default="root@example.com")

    def handle(self, *args, **opts):
        try:
            default_shard = Shard.objects.get(is_default=True)
        except Shard.DoesNotExist:
            raise CommandError(
                "No default shard registered. Run `migrate_schemas --shared "
                "--database=default` and `sync_shards --activate` first."
            )

        tenant, created = Tenant.objects.get_or_create(
            schema_name="public",
            defaults={
                "company_name": "Public",
                "shard": default_shard,
                "status": Tenant.Status.ACTIVE,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Created public tenant."))
        # Normalize + format-check BEFORE the row is written. get_or_create bypasses
        # full_clean(), and Domain.clean() exempts the public tenant anyway, so without
        # this a stray case / trailing dot / copy-pasted space would be stored verbatim
        # and never match an incoming Host (request.get_host() returns the raw header;
        # the column compares case-sensitively) - an unreachable management host with a
        # success message. validate_hostname only checks FORMAT: the deliberate
        # reserved-host exemption for the public tenant (see Domain.clean) is preserved.
        try:
            domain = validate_hostname(opts["domain"])
        except ValidationError as exc:
            raise CommandError(f"--domain: {'; '.join(exc.messages)}")

        Domain.objects.get_or_create(
            domain=domain,
            defaults={"tenant": tenant, "is_primary": True},
        )

        # The admin lives in the PUBLIC schema on the default connection. Establish that
        # context explicitly (current_db -> "default", schema -> public) so the INSERT is
        # routed deterministically — and so it survives `users` being in TENANT_STRICT_ROUTE_APPS
        # (an unset context would otherwise raise in the strict router).
        with schema_context(get_public_schema_name()):
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
                f"Tenant-admin '{user.username}' ready on host '{domain}'."
            )
        )