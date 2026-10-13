"""reconcile_tenants — read-only migration state of public and the tenants
(deploy/migrations_plan_rev7.md §11). Never writes: statuses change only through the
migrator (fenced CAS), the admin and the console.

  --check                    deploy gate. Exit 1 if public is behind, the code is older
                             than the database, any ACTIVE tenant is behind or has no
                             schema, or `default` / a shard holding ACTIVE tenants is
                             unreachable. A stuck PENDING is a WARN only. NEW, DEACTIVATED,
                             FAILED and MAINTENANCE are not checked.
  --report [-s X] [--only-pending] [--plan]
                             one row per tenant (public first): status, migrations behind
                             or "no schema", lease (owner, since, EXPIRED), PENDING in
                             progress or stuck, INVALID indexes, and the next command to
                             run. --plan adds the names of the unapplied migrations.

"Behind" is migrator.behind() — the same definition migrate_schemas' crash snapshot uses.
"""
from __future__ import annotations

from typing import Any

from django.core.management.base import CommandError, CommandParser
from django_tenants.utils import get_public_schema_name

from commons.platform.commands import TenantCommand
from tenants import migrator
from tenants.models import Tenant


class Command(TenantCommand):
    help = ("Read-only migration state (deploy/migrations_plan_rev7.md §11): --check is the "
            "deploy gate (exit 1 on any problem), --report is the per-tenant table.")

    def add_arguments(self, parser: CommandParser) -> None:
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--check", action="store_true",
                       help="Deploy gate: exit 1 if public or any ACTIVE tenant is not up to date.")
        g.add_argument("--report", action="store_true", help="Per-tenant state table.")
        parser.add_argument("-s", "--schema", dest="schema", help="--report: only this tenant.")
        parser.add_argument("--only-pending", action="store_true", dest="only_pending",
                            help="--report: only PENDING tenants.")
        parser.add_argument("--plan", action="store_true",
                            help="--report: list the unapplied migrations by name.")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["check"] and (options["schema"] or options["only_pending"] or options["plan"]):
            raise CommandError("-s, --only-pending and --plan go only with --report.")
        try:
            self.holders = migrator.lease_holders()
        except Exception as e:
            if not migrator.is_conn_error(e):
                raise
            raise CommandError(f"default unreachable: {e}")
        if options["check"]:
            self._check()
        else:
            self._report(options["schema"], options["only_pending"], options["plan"])

    # ------------------------------------------------------------------
    # Shared pieces
    # ------------------------------------------------------------------
    def _pending_state(self, t: Tenant) -> tuple[bool, str]:
        """(stuck, why) for a PENDING tenant: in progress while its lease is live or a
        tm:<pk> backend is still active (expired lease, or a lease released after a lost
        connection — the migrator will not take it until that backend is gone); stuck
        otherwise."""
        h = self.holders.get(t.pk)
        if h is not None and not h.expired:
            return False, f"in progress (lease {h.owner})"
        s = migrator.shard_session_active(t.shard.alias, t.pk)
        lease = "lease expired" if h is not None else "no lease"
        if s is not None:
            return False, f"in progress ({lease}, backend pid {s.pid} still active)"
        return True, f"stuck ({lease}, no active backend)"

    @staticmethod
    def _tenants(**filters: Any):
        return (Tenant.objects.select_related("shard").filter(**filters)
                .exclude(schema_name=get_public_schema_name()).order_by("shard__alias", "schema_name"))

    # ------------------------------------------------------------------
    # --check
    # ------------------------------------------------------------------
    def _check(self) -> None:
        problems: list[str] = []
        try:
            if migrator.code_older_than_db():
                problems.append("code_older_than_db: public holds migrations this code does not "
                                "know, applied by a newer release — deploy the newer code")
            plan = migrator.public_plan()
            if plan:
                problems.append(f"public: {len(plan)} migration(s) behind — run "
                                f"`migrate_schemas --shared`")
            active = list(self._tenants(status=Tenant.Status.ACTIVE))
            pending = list(self._tenants(status=Tenant.Status.PENDING))
        except Exception as e:
            if not migrator.is_conn_error(e):
                raise
            raise CommandError(f"reconcile_tenants --check: default unreachable: {e}")

        n_behind = n_missing = n_stuck = 0
        unreachable: set[str] = set()
        for t in active:
            alias = t.shard.alias
            if alias in unreachable:
                continue
            try:
                b = migrator.behind(t)
            except Exception as e:
                if not migrator.is_conn_error(e):
                    raise
                unreachable.add(alias)
                problems.append(f"shard {alias} unreachable: {e}".rstrip())
                continue
            if b is None:
                n_missing += 1
                problems.append(f"{t.schema_name} ({alias}): ACTIVE but its schema is missing")
            elif b:
                n_behind += 1
                hint = ("" if t.shard.is_active else
                        f"; shard {alias} is inactive, so `--tenant` skips it")
                problems.append(f"{t.schema_name} ({alias}): {b} migration(s) behind — run "
                                f"`migrate_schemas -s {t.schema_name}`{hint}")
        for t in pending:
            try:
                stuck, why = self._pending_state(t)
            except Exception as e:
                if not migrator.is_conn_error(e):
                    raise
                stuck, why = True, f"unknown (shard {t.shard.alias} unreachable)"
            if stuck:
                n_stuck += 1
                self.stdout.write(f"WARN stuck PENDING {t.schema_name} ({t.shard.alias}): {why}")

        for p in problems:
            self.stdout.write(f"FAIL {p}")
        self.stdout.write(f"checked_active={len(active)} behind={n_behind} missing={n_missing} "
                          f"pending_stuck={n_stuck} unreachable_shards={sorted(unreachable)}")
        if problems:
            raise CommandError(f"reconcile_tenants --check: {len(problems)} problem(s).", returncode=1)
        self.stdout.write("OK")

    # ------------------------------------------------------------------
    # --report
    # ------------------------------------------------------------------
    def _report(self, schema: str | None, only_pending: bool, names: bool) -> None:
        filters: dict[str, Any] = {}
        if schema:
            filters["schema_name"] = schema
        if only_pending:
            filters["status"] = Tenant.Status.PENDING
        tenants = list(self._tenants(**filters))
        if schema and not tenants:
            raise CommandError(f"Tenant {schema!r} not found"
                               f"{' (or not PENDING)' if only_pending else ''}.")
        self.stdout.write(f"{'SCHEMA':<24} {'SHARD':<10} {'STATUS':<12} {'BEHIND':<10} {'LEASE':<44} NEXT")
        if not schema and not only_pending:
            self._public_row(names)
        unreachable: set[str] = set()
        for t in tenants:
            self._tenant_row(t, names, unreachable)

    def _lease_text(self, key: int) -> str:
        h = self.holders.get(key)
        if h is None:
            return "-"
        return f"{h.owner} since {h.since:%Y-%m-%dT%H:%M:%SZ}{' EXPIRED' if h.expired else ''}"

    def _row(self, schema: str, shard: str, status: str, behind: str, key: int,
             next_: str, notes: list[str], plan: list[tuple[str, str]] | None) -> None:
        self.stdout.write(f"{schema:<24} {shard:<10} {status:<12} {behind:<10} "
                          f"{self._lease_text(key):<44} {next_}")
        for n in notes:
            self.stdout.write(f"{'':<24} {n}")
        for app, name in plan or ():
            self.stdout.write(f"{'':<24}   - {app}.{name}")

    def _public_row(self, names: bool) -> None:
        try:
            plan = migrator.public_plan()
        except Exception as e:
            if not migrator.is_conn_error(e):
                raise
            self._row("public", "default", "-", "?", 0, "default unreachable", [str(e)], None)
            return
        if 0 in self.holders:
            nxt = self._lease_next(0, "shared", ["default"])
        else:
            nxt = "migrate_schemas --shared" if plan else "-"
        self._row("public", "default", "-", str(len(plan)), 0, nxt, [], plan if names else None)

    def _lease_next(self, key: int, target: str, aliases: list[str]) -> str:
        """Next step while lease `key` is held."""
        h = self.holders[key]
        if not h.expired:
            return f"wait: migration in progress ({h.owner})"
        for alias in aliases:
            s = migrator.shard_session_active(alias, key)
            if s is not None:
                return f"wait: lease expired, backend pid {s.pid} on {alias} still active"
        return f"migration_lease --release {target}, then rerun"

    def _tenant_row(self, t: Tenant, names: bool, unreachable: set[str]) -> None:
        alias, schema, status = t.shard.alias, t.schema_name, t.status
        if alias in unreachable:
            self._row(schema, alias, status, "?", t.pk, "shard unreachable", [], None)
            return
        notes: list[str] = []
        plan: list[tuple[str, str]] | None = None
        try:
            p = migrator.pending_plan(t)
            behind = "no schema" if p is migrator.MISSING else str(len(p))
            if p is not migrator.MISSING and names:
                plan = p
            invalid = migrator.invalid_indexes(alias, schema) if p is not migrator.MISSING else ()
            if status == Tenant.Status.PENDING:
                notes.append(f"PENDING {self._pending_state(t)[1]}")
            nxt = self._next(t, p, invalid)
        except Exception as e:
            if not migrator.is_conn_error(e):
                raise
            unreachable.add(alias)
            self._row(schema, alias, status, "?", t.pk, "shard unreachable", [str(e).strip()], None)
            return
        if invalid:
            notes.append(f"INVALID indexes: {', '.join(invalid)}")
        if status == Tenant.Status.FAILED and t.last_error:
            notes.append(f"last_error: {t.last_error.strip().splitlines()[-1][:160]}")
        self._row(schema, alias, status, behind, t.pk, nxt, notes, plan)

    def _next(self, t: Tenant, plan: Any, invalid: tuple[str, ...]) -> str:
        s, schema = t.status, t.schema_name
        if t.pk in self.holders:
            return self._lease_next(t.pk, schema, [t.shard.alias])
        if s == Tenant.Status.FAILED:
            return "admin: FAILED → NEW (see last_error)"
        if s == Tenant.Status.MAINTENANCE:
            return (f"operator-owned: after the repair `migrate_schemas -s {schema} "
                    f"--maintenance`, then admin → ACTIVE")
        if invalid:
            return f"DROP INDEX CONCURRENTLY each INVALID index, then `migrate_schemas -s {schema}`"
        if plan is migrator.MISSING:
            if s in migrator.PROVISIONABLE:
                return f"migrate_schemas -s {schema}"
            return (f"schema missing: restore it (runbook §19, via MAINTENANCE); "
                    f"`migrate_schemas -s {schema}` would mark it FAILED schema_missing")
        if plan or s in migrator.PROVISIONABLE:
            return f"migrate_schemas -s {schema}"
        return "-"
