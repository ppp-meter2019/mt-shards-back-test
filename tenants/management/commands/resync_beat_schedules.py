"""Force beat to re-read ALL tenants' periodic-task schedules.

Bumps every tenant's beat change-marker so a running beat reloads the full
schedule on its next tick. Use after a DIRECT DB injection (raw SQL / restore /
bulk load) that bypassed Django ORM signals, or any time you want a guaranteed
full resync. If beat is not running it reloads everything on next start anyway.
"""
from django.core.management.base import BaseCommand

from tenants.celery.change_marker import bump_all


class Command(BaseCommand):
    help = "Force beat to re-read ALL tenants' periodic-task schedules."

    def handle(self, *args, **options):
        n = bump_all()
        self.stdout.write(self.style.SUCCESS(
            f"Bumped {n} beat markers (public + tenants) — a running beat will fully "
            f"reload all schedules on its next tick."
        ))
