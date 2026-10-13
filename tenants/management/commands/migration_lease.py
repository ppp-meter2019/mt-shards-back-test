"""migration_lease — inspect and release migrator leases (deploy/migrations_plan_rev7.md §10).

  --list                     every held lease: key, schema (`shared` for key 0, `tenant
                             deleted` when the tenant is gone), owner, since, until,
                             EXPIRED, and whether a backend labelled tm:<key> is still
                             active on the shard
  --release <target>         free an EXPIRED lease, unless a tm:<key> backend is still
                             active on the shard (then refuse with its pid, run time and
                             query). A lease that has not expired yet is refused too: its
                             owner may be alive and merely between two statements, which
                             pg_stat_activity shows as idle
  --release <target> --force free it without those checks — only when the owning process
                             is certainly dead and no backend is left (runbook §19)

<target> is a schema name, `shared` (the --shared stage, key 0) or `key=<n>` (a lease
whose tenant was deleted; --list prints the exact form).

The release is fenced on the owner shown to the operator: a run that re-took the lease in
between is never touched. Every read/write goes through tenants.migrator.
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import CommandError, CommandParser

from commons.platform.commands import TenantCommand
from tenants import migrator
from tenants.models import Shard, Tenant

SHARED = "shared"


class Command(TenantCommand):
    help = ("List or release migrator leases (deploy/migrations_plan_rev7.md §10). "
            "--release refuses while the lease's backend is still active; --force skips that.")

    def add_arguments(self, parser: CommandParser) -> None:
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--list", action="store_true", help="List every held lease.")
        g.add_argument("--release", metavar="TARGET",
                       help="Release one lease: <schema>, 'shared' or key=<n>.")
        parser.add_argument("--force", action="store_true",
                            help="With --release: skip the active-backend check.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["force"] and not options["release"]:
            raise CommandError("--force goes only with --release.")
        if options["list"]:
            self._list()
        else:
            self._release(options["release"], options["force"])

    # ------------------------------------------------------------------
    def _resolve(self, target: str) -> tuple[int, str, list[str]]:
        """(key, label, shard aliases to look for a tm:<key> backend on)."""
        if target.startswith("key="):
            try:
                key = int(target[4:])
            except ValueError:
                raise CommandError(f"{target!r}: key=<n> needs an integer.")
            if key == 0:
                return 0, SHARED, ["default"]
            t = Tenant.objects.select_related("shard").filter(pk=key).first()
            if t is not None:
                return key, t.schema_name, [t.shard.alias]
            return key, f"key={key} (tenant deleted)", self._all_shards()
        t = Tenant.objects.select_related("shard").filter(schema_name=target).first()
        if target == SHARED:
            if t is not None:
                raise CommandError(f"{SHARED!r} is ambiguous: a tenant has that schema name. "
                                   f"Use key=0 for the --shared stage or key={t.pk} for the tenant.")
            return 0, SHARED, ["default"]
        if t is None:
            raise CommandError(f"Tenant {target!r} not found (for a deleted tenant's lease use "
                               f"key=<n> from --list).")
        return t.pk, t.schema_name, [t.shard.alias]

    @staticmethod
    def _all_shards() -> list[str]:
        """The tenant is gone, so is the record of its shard: look on every shard."""
        return list(Shard.objects.filter(is_default=False).values_list("alias", flat=True))

    @staticmethod
    def _session(aliases: list[str], key: int) -> tuple[str | None, migrator.ShardSession | None]:
        """First active tm:<key> backend on `aliases`; raises on an unreachable shard."""
        for alias in aliases:
            s = migrator.shard_session_active(alias, key)
            if s is not None:
                return alias, s
        return None, None

    @staticmethod
    def _fmt_session(alias: str, s: migrator.ShardSession) -> str:
        return (f"pid {s.pid} on {alias}, running {int(s.running.total_seconds() // 60)} min: "
                f"{s.query}")

    # ------------------------------------------------------------------
    def _list(self) -> None:
        holders = migrator.lease_holders()
        if not holders:
            self.stdout.write("No leases held.")
            return
        tenants = {t.pk: t for t in Tenant.objects.select_related("shard").filter(pk__in=list(holders))}
        self.stdout.write(f"{'KEY':>6}  {'SCHEMA':<24} {'OWNER':<44} {'SINCE':<20} {'UNTIL':<20} "
                          f"{'STATE':<8} SESSION")
        for key, h in holders.items():
            if key == 0:
                label, aliases = SHARED, ["default"]
            elif key in tenants:
                label, aliases = tenants[key].schema_name, [tenants[key].shard.alias]
            else:
                label, aliases = f"tenant deleted (key={key})", self._all_shards()
            try:
                alias, s = self._session(aliases, key)
                session = self._fmt_session(alias, s) if s is not None else "none"
            except Exception as e:
                if not migrator.is_conn_error(e):
                    raise
                session = f"? shard unreachable ({type(e).__name__})"
            self.stdout.write(
                f"{key:>6}  {label:<24} {h.owner:<44} {h.since:%Y-%m-%dT%H:%M:%SZ}  "
                f"{h.until:%Y-%m-%dT%H:%M:%SZ}  {'EXPIRED' if h.expired else 'live':<8} {session}")

    def _release(self, target: str, force: bool) -> None:
        key, label, aliases = self._resolve(target)
        h = migrator.lease_holder(key)
        if h is None:
            self.stdout.write(f"Lease of {label} (key {key}) is not held; nothing to release.")
            return
        if not force:
            try:
                alias, s = self._session(aliases, key)
            except Exception as e:
                if not migrator.is_conn_error(e):
                    raise
                raise CommandError(
                    f"Cannot check {', '.join(aliases)} for an active tm:{key} backend ({e}). "
                    f"Not released. Use --force only if the owner is certainly dead and no "
                    f"backend is left.")
            if s is not None:
                raise CommandError(
                    f"Not released: the migration of {label} is still running server-side "
                    f"({self._fmt_session(alias, s)}). Wait for it to finish, or cancel it "
                    f"with pg_cancel_backend({s.pid}) on {alias}, then retry.")
            if not h.expired:
                raise CommandError(
                    f"Not released: the lease of {label} is live until "
                    f"{h.until:%Y-%m-%dT%H:%M:%SZ} (owner {h.owner}). Its owner may be "
                    f"alive between two statements, which no backend check can see. Wait "
                    f"for it to expire, or — only if that process is certainly dead (check "
                    f"`ps -o lstart= -p <pid>` on its host) — repeat with --force.")
        if not migrator.lease_force_release(key, h.owner):
            raise CommandError(f"Lease of {label} changed hands meanwhile (now: "
                               f"{getattr(migrator.lease_holder(key), 'owner', 'free')}); "
                               f"nothing released. Check with --list.")
        self.stdout.write(
            f"Released lease of {label} (key {key}): owner {h.owner}, since "
            f"{h.since:%Y-%m-%dT%H:%M:%SZ}, until {h.until:%Y-%m-%dT%H:%M:%SZ}"
            f"{' (EXPIRED)' if h.expired else ''}{' [--force]' if force else ''}.")
