"""Bootstrap helper: create Shard rows for aliases declared in settings.DATABASES."""

from django.conf import settings
from django.core.management.base import BaseCommand

from tenants.models import Shard


class Command(BaseCommand):
    help = "Create Shard rows for any settings.DATABASES aliases that aren't yet registered."

    def add_arguments(self, parser):
        parser.add_argument(
            "--activate", action="store_true",
            help="Mark new non-default shards as is_active=True.",
        )

    def handle(self, *args, **opts):
        existing = set(Shard.objects.values_list("alias", flat=True))
        created = 0
        for alias in settings.DATABASES:
            if alias in existing:
                continue
            is_default = (alias == "default")
            Shard.objects.create(
                alias=alias,
                name=alias.replace("_", " ").title(),
                is_default=is_default,
                is_active=is_default or opts["activate"],
            )
            created += 1
            self.stdout.write(self.style.SUCCESS(
                f"Created shard {alias} (is_default={is_default})"
            ))
        if not created:
            self.stdout.write("All aliases already registered.")
