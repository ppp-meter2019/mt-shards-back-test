"""List offline-coordinates objects stored in S3, with their sizes.

Companion to loadtest_offline_coordinates_s3 - inspect what was uploaded.

    # everything under offline-coordinates/
    python manage.py list_offline_coordinates_s3

    # one tenant only (by schema_name or numeric id), newest 50
    python manage.py list_offline_coordinates_s3 --tenant acme --limit 50

    # an arbitrary prefix (e.g. a single day)
    python manage.py list_offline_coordinates_s3 --prefix offline-coordinates/tenant=3/dt=2026-07-13/
"""

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name

from routes.services.service_offline_coordinates_s3_uploader import (
    list_offline_coordinates_objects,
)
from tenants.models import Tenant

_UNITS = ("B", "KB", "MB", "GB", "TB")


def _human(size: int) -> str:
    value = float(size)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


class Command(BaseCommand):
    help = "List offline-coordinates objects in S3 with their sizes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            help="Restrict to this tenant (schema_name or numeric id).",
        )
        parser.add_argument(
            "--prefix",
            help="Explicit key prefix. Overrides --tenant when both are given.",
        )
        parser.add_argument(
            "--limit", type=int,
            help="Show at most this many objects.",
        )

    def _resolve_tenant_id(self, raw):
        raw = raw.strip()
        qs = Tenant.objects.exclude(schema_name=get_public_schema_name())
        lookup = {"pk": raw} if raw.isdigit() else {"schema_name": raw}
        try:
            return qs.values_list("id", flat=True).get(**lookup)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant {raw!r} not found (or is the public tenant).")

    def handle(self, *args, **opts):
        if opts["limit"] is not None and opts["limit"] < 1:
            raise CommandError("--limit must be >= 1.")

        tenant_id = None
        if opts["prefix"] is None and opts["tenant"]:
            tenant_id = self._resolve_tenant_id(opts["tenant"])

        scope = opts["prefix"] or (
            f"tenant={tenant_id}" if tenant_id is not None else "all tenants"
        )
        self.stdout.write(f"Listing offline coordinates ({scope}):")

        count = 0
        total = 0
        for obj in list_offline_coordinates_objects(
            tenant_id=tenant_id, prefix=opts["prefix"], limit=opts["limit"]
        ):
            count += 1
            total += obj["size"]
            self.stdout.write(
                f"  {obj['last_modified']:%Y-%m-%d %H:%M:%S}  "
                f"{_human(obj['size']):>11}  {obj['key']}"
            )

        if count == 0:
            self.stdout.write(self.style.WARNING("  (no objects found)"))
            return

        self.stdout.write("-" * 72)
        self.stdout.write(
            self.style.SUCCESS(f"{count} object(s), total {_human(total)}.")
        )
