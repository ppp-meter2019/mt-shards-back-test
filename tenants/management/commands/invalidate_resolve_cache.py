"""Invalidate CACHES["tenant_resolve"] entries.

    manage.py invalidate_resolve_cache --ids 1 2 3       # by Tenant pk
    manage.py invalidate_resolve_cache --names alpha beta # by Tenant.name
    manage.py invalidate_resolve_cache --all              # everything (prefix-scoped)

Fails loudly (CommandError) if Redis is unreachable, even under IGNORE_EXCEPTIONS.
"""
from django.core.management.base import BaseCommand, CommandError

from tenants.resolve_cache import CacheUnavailable, resolve_cache


class Command(BaseCommand):
    help = "Invalidate tenant-resolution cache entries (by id, by name, or all)."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--ids", nargs="+", type=int, help="Tenant pk(s).")
        group.add_argument("--names", nargs="+", help="Tenant name(s) (Tenant.name).")
        group.add_argument("--all", action="store_true", help="Invalidate ALL entries.")

    def handle(self, *args, **opts):
        if not resolve_cache.redis_alive():
            raise CommandError("tenant_resolve Redis is not reachable")
        try:
            if opts["all"]:
                n = resolve_cache.forget_all(raise_on_error=True)
                msg = f"invalidated {n} entries"
            elif opts["ids"]:
                n = resolve_cache.forget_ids(opts["ids"], raise_on_error=True)
                msg = f"invalidated {n} host(s) for {len(opts['ids'])} tenant id(s)"
            else:
                n = resolve_cache.forget_names(opts["names"], raise_on_error=True)
                msg = f"invalidated {n} host(s) for {len(opts['names'])} tenant name(s)"
        except CacheUnavailable as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"tenant_resolve: {msg}"))