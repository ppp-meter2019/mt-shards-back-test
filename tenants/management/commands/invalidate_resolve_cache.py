"""Invalidate CACHES["tenant_resolve"] entries.

    manage.py invalidate_resolve_cache --ids 1 2 3         # by Tenant pk
    manage.py invalidate_resolve_cache --schemas alpha beta # by Tenant.schema_name
    manage.py invalidate_resolve_cache --all                # everything (prefix-scoped)

--all is GATE-AWARE: with TENANT_REGISTRY["WARM_ENABLED"] on, a bare forget_all would leave
the `treg:hosts` SET behind → every miss becomes an uncapped member cold-fill (a herd on
`default`). So under WARM it refreshes via reconcile (force-overwrite in place + RENAME —
no herd, no gap) instead. --ids/--schemas only tombstone specific hosts (the SET is
unchanged, those hosts re-fill), so they are safe under the gate as-is.

Fails loudly (CommandError) if Redis is unreachable, even under IGNORE_EXCEPTIONS.
"""

from typing import Any

from django.core.management.base import CommandError, CommandParser

from commons.platform.commands import TenantCommand

from tenants.resolver import CacheUnavailable, flags, resolve_cache


class Command(TenantCommand):
    help = "Invalidate tenant-resolution cache entries (by id, by schema_name, or all)."

    def add_arguments(self, parser: CommandParser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--ids", nargs="+", type=int, help="Tenant pk(s).")
        group.add_argument("--schemas", nargs="+", help="Tenant schema_name(s).")
        group.add_argument("--all", action="store_true", help="Invalidate ALL entries.")

    def handle(self, *args: Any, **opts: Any) -> None:
        if not resolve_cache.redis_alive():
            raise CommandError("tenant_resolve Redis is not reachable")
        try:
            if opts["all"]:
                if flags.warm_enabled():
                    # Gate-aware: refresh via reconcile (no herd) rather than a bare wipe
                    # that would leave treg:hosts and stampede `default` with cold-fills.
                    from tenants.resolver import host_registry
                    n = host_registry.run_locked()
                    msg = ("another writer holds the reconcile lock; skipped"
                           if n is None else f"reconciled {n} host(s) (gate-aware refresh)")
                else:
                    n = resolve_cache.forget_all(raise_on_error=True)
                    msg = f"invalidated {n} entries"
            elif opts["ids"]:
                n = resolve_cache.forget_ids(opts["ids"], raise_on_error=True)
                msg = f"invalidated {n} host(s) for {len(opts['ids'])} tenant id(s)"
            else:
                n = resolve_cache.forget_schemas(opts["schemas"], raise_on_error=True)
                msg = f"invalidated {n} host(s) for {len(opts['schemas'])} schema(s)"
        except CacheUnavailable as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"tenant_resolve: {msg}"))