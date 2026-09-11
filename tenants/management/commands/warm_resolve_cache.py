"""Preload / rebuild CACHES["tenant_resolve"] for all tenants.

    manage.py warm_resolve_cache            # fill only ABSENT entries (nx, idempotent)
    manage.py warm_resolve_cache --force    # hard reload: overwrite every entry

GATE-AWARE — a single entry point so an operator can't run the wrong warm:
  * TENANT_REGISTRY["WARM_ENABLED"] on → runs the registry RECONCILE (force-overwrite by
    ttl_by_status, skip holds, build the `treg:hosts` SET, orphan-sweep, single-writer
    lock). --force is implied; the flag is ignored.
  * off → legacy positive-cache warm (flat TTL, no SET) — today's behavior.

Fails loudly (CommandError) if Redis is unreachable — even under IGNORE_EXCEPTIONS,
so a "warm" that silently did nothing can't pass for success.
"""

from typing import Any

from django.core.management.base import CommandError, CommandParser

from commons.platform.commands import TenantCommand

from tenants.resolver import CacheUnavailable, flags, resolve_cache


class Command(TenantCommand):
    help = "Preload / rebuild the tenant-resolution cache (reconcile when the gate is on)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--force", action="store_true",
            help='Hard reload (overwrite all); default fills only absent entries. '
                 'Ignored when TENANT_REGISTRY["WARM_ENABLED"] is on (reconcile always '
                 'force-overwrites).',
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        if not resolve_cache.redis_alive():
            raise CommandError("tenant_resolve Redis is not reachable")

        if flags.warm_enabled():
            # Under the gate, reconcile is the ONLY correct warm (builds the SET +
            # ttl_by_status). It supersedes the positive-only warm in the else-branch, which
            # now REFUSES to run under WARM (TenantResolveCache.LegacyWarmRefused) — so this
            # branch is enforced, not merely conventional.
            #
            # SUNSET: when WARM becomes permanent, the else-branch and
            # TenantResolveCache.warm() both go away and this command simply IS reconcile.
            # Until then the gate-off warm is the LIVE path for the default configuration
            # (TENANT_REGISTRY["WARM_ENABLED"] defaults to False), so it is not dead code.
            from tenants.resolver import host_registry
            n = host_registry.run_locked()
            if n is None:
                self.stdout.write(self.style.WARNING(
                    "tenant_resolve: another writer holds the reconcile lock — skipped"))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"tenant_resolve: reconciled {n} host(s) (gate registry + snapshots)"))
            return

        try:
            n = resolve_cache.warm(force=opts["force"], raise_on_error=True)
        except CacheUnavailable as exc:
            raise CommandError(str(exc))
        verb = "reloaded" if opts["force"] else "filled"
        self.stdout.write(self.style.SUCCESS(f"tenant_resolve: {verb} {n} entries (legacy warm)"))
