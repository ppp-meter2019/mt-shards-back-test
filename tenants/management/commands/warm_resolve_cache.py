"""Preload CACHES["tenant_resolve"] for all tenants.

    manage.py warm_resolve_cache            # fill only ABSENT entries (nx, idempotent)
    manage.py warm_resolve_cache --force    # hard reload: overwrite every entry

Fails loudly (CommandError) if Redis is unreachable — even under IGNORE_EXCEPTIONS,
so a "warm" that silently did nothing can't pass for success.
"""
from django.core.management.base import BaseCommand, CommandError

from tenants.resolve_cache import CacheUnavailable, resolve_cache


class Command(BaseCommand):
    help = "Preload the tenant-resolution cache for all tenants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Hard reload (overwrite all); default fills only absent entries.",
        )

    def handle(self, *args, **opts):
        if not resolve_cache.redis_alive():
            raise CommandError("tenant_resolve Redis is not reachable")
        try:
            n = resolve_cache.warm(force=opts["force"], raise_on_error=True)
        except CacheUnavailable as exc:
            raise CommandError(str(exc))
        verb = "reloaded" if opts["force"] else "filled"
        self.stdout.write(self.style.SUCCESS(f"tenant_resolve: {verb} {n} entries"))