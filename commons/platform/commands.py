"""Base class for tenant-management commands.

Refuses to run when USE_MULTITENANT is off, with a clear message. In standalone the
`tenants` app is not installed, so its commands are absent anyway — this is a
belt-and-suspenders guard (and a friendly error) for a half-configured environment.
Subclass this instead of BaseCommand in every tenant-only management command.
"""
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class TenantCommand(BaseCommand):
    def execute(self, *args: Any, **options: Any) -> Any:
        if not settings.USE_MULTITENANT:
            raise CommandError(
                f"{type(self).__module__}: this command requires USE_MULTITENANT=True "
                f"(it operates on tenant schemas, which do not exist in standalone mode)."
            )
        return super().execute(*args, **options)
