"""Reconcile Tenant.status with the actual migration state on each shard.

Strategy: conservative.
  - Auto-fixes only obvious drifts (a stale PENDING that actually completed).
  - Never overrides ACTIVE/FAILED/DEACTIVATED automatically.
  - 'ACTIVE but behind on migrations' is REPORTED, not changed - it is the
    normal state immediately after a new code deploy until the operator runs
    `migrate_schemas --tenant`.
"""

from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, connections
from django.db.migrations.loader import MigrationLoader
from django.db.utils import ConnectionDoesNotExist
from django.utils import timezone
from django_tenants.utils import schema_exists

from tenants.models import Tenant


@dataclass
class Decision:
    new_status: str
    status_change: bool
    reason: str = ""
    warning: str = ""
    error: str = ""


class Command(BaseCommand):
    help = "Reconcile Tenant.status with actual migration state on each shard."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Only print what would be changed; do not write.")
        parser.add_argument("--report", action="store_true",
                            help="Print a status table (read-only).")
        parser.add_argument("--only-pending", action="store_true",
                            help="Only inspect tenants whose status=pending.")
        parser.add_argument("--schema", help="Restrict to a single schema_name.")

    def handle(self, *args, **opts):
        if opts["report"]:
            self._print_report(opts.get("schema"))
            return

        qs = self._build_queryset(opts)
        changed = warned = errored = 0

        for tenant in qs:
            decision = self._evaluate(tenant)

            if decision.error:
                errored += 1
                self.stdout.write(self.style.ERROR(
                    f"ERROR {tenant.schema_name}: {decision.error}"
                ))

            if decision.status_change:
                changed += 1
                marker = "DRY-RUN " if opts["dry_run"] else ""
                self.stdout.write(self.style.SUCCESS(
                    f"{marker}FIX  {tenant.schema_name}: "
                    f"{tenant.status} -> {decision.new_status}  ({decision.reason})"
                ))
                if not opts["dry_run"]:
                    self._apply_status(tenant, decision.new_status, decision.reason)
            elif decision.warning:
                warned += 1
                self.stdout.write(self.style.WARNING(
                    f"WARN {tenant.schema_name}: {decision.warning}"
                ))

        suffix = " (dry-run - no changes saved)" if opts["dry_run"] else ""
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Reconcile complete{suffix}: "
            f"{changed} change(s), {warned} warning(s), {errored} error(s)."
        ))

    def _build_queryset(self, opts):
        qs = Tenant.objects.select_related("shard").exclude(schema_name="public")
        if opts["only_pending"]:
            qs = qs.filter(status=Tenant.Status.PENDING)
        if opts["schema"]:
            qs = qs.filter(schema_name=opts["schema"])
        return qs

    # ------------------------------------------------------------------
    # Decision logic - conservative
    # ------------------------------------------------------------------
    def _evaluate(self, tenant) -> Decision:
        # Sanity: alias must be in settings.DATABASES.
        try:
            conn = connections[tenant.shard.alias]
        except ConnectionDoesNotExist:
            return Decision(
                new_status=tenant.status, status_change=False,
                error=(f"shard alias {tenant.shard.alias!r} is not configured "
                       f"in settings.DATABASES"),
            )

        # schema_exists() takes the database ALIAS (string), not a connection.
        exists = schema_exists(tenant.schema_name, tenant.shard.alias)
        if not exists:
            return self._eval_no_schema(tenant)

        applied = self._applied(conn, tenant.schema_name)
        expected = self._expected()

        if applied >= expected:
            return self._eval_fully_migrated(tenant)
        return self._eval_partial(tenant, applied, expected)

    def _eval_no_schema(self, tenant) -> Decision:
        status = tenant.status
        if status == Tenant.Status.PENDING:
            # Claim succeeded but CREATE SCHEMA never ran. Roll the claim back.
            return Decision(
                new_status=Tenant.Status.NEW, status_change=True,
                reason="Schema not created; rolling back stale pending.",
            )
        if status == Tenant.Status.ACTIVE:
            # ACTIVE without a schema = serving requests with no DB - red alert.
            return Decision(
                new_status=status, status_change=False,
                error="ACTIVE but schema is missing - likely data loss; manual recovery required.",
            )
        return Decision(new_status=status, status_change=False)

    def _eval_fully_migrated(self, tenant) -> Decision:
        status = tenant.status
        if status == Tenant.Status.PENDING:
            # Migrations completed but the final status flip was lost.
            target = (Tenant.Status.DEACTIVATED
                      if tenant.previous_status == Tenant.Status.DEACTIVATED
                      else Tenant.Status.ACTIVE)
            return Decision(
                new_status=target, status_change=True,
                reason="All migrations applied; finalizing stale pending.",
            )
        if status == Tenant.Status.NEW:
            # Migrations applied out-of-band (DBA used create_tenant_schema +
            # ran migrate manually). Sync the status.
            return Decision(
                new_status=Tenant.Status.ACTIVE, status_change=True,
                reason="Schema fully migrated externally; marking active.",
            )
        return Decision(new_status=status, status_change=False)

    def _eval_partial(self, tenant, applied, expected) -> Decision:
        status = tenant.status
        progress = f"{applied}/{expected}"

        if status == Tenant.Status.ACTIVE:
            # Common state after a deploy adds new TENANT_APPS migrations.
            # Operator needs to run migrate_schemas - we do NOT mark as failed.
            return Decision(
                new_status=status, status_change=False,
                warning=(f"ACTIVE but behind on migrations ({progress}); "
                         f"run `manage.py migrate_schemas --tenant`."),
            )
        if status == Tenant.Status.PENDING:
            return Decision(
                new_status=status, status_change=False,
                warning=(f"PENDING with partial migrations ({progress}); "
                         f"manual review - likely interrupted run."),
            )
        if status == Tenant.Status.NEW:
            return Decision(
                new_status=status, status_change=False,
                warning=(f"NEW but schema partial ({progress}); "
                         f"likely failed initial provisioning."),
            )
        if status == Tenant.Status.DEACTIVATED:
            return Decision(
                new_status=status, status_change=False,
                warning=(f"DEACTIVATED, behind on migrations ({progress}); informational."),
            )
        # FAILED + partial - that's the expected failed state.
        return Decision(new_status=status, status_change=False)

    # ------------------------------------------------------------------
    def _apply_status(self, tenant, new_status, reason):
        update_fields = {
            "status": new_status,
            "status_changed_at": timezone.now(),
        }
        if new_status in (Tenant.Status.ACTIVE, Tenant.Status.DEACTIVATED):
            update_fields["last_error"] = ""
        else:
            update_fields["last_error"] = reason
        Tenant.objects.filter(pk=tenant.pk).update(**update_fields)

    # ------------------------------------------------------------------
    # Migration introspection
    # ------------------------------------------------------------------
    def _applied(self, conn, schema_name) -> int:
        labels = self._tenant_app_labels()
        if not labels:
            return 0
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema_name}", public')
            cur.execute(
                "SELECT COUNT(*) FROM django_migrations WHERE app = ANY(%s)",
                [list(labels)],
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def _expected(self) -> int:
        if hasattr(self, "_expected_cache"):
            return self._expected_cache
        loader = MigrationLoader(connection)
        labels = self._tenant_app_labels()
        self._expected_cache = sum(
            1 for (app, _) in loader.graph.nodes if app in labels
        )
        return self._expected_cache

    def _tenant_app_labels(self) -> set:
        if hasattr(self, "_labels_cache"):
            return self._labels_cache
        self._labels_cache = {
            a.rsplit(".", 1)[-1] for a in settings.TENANT_APPS
        }
        return self._labels_cache

    # ------------------------------------------------------------------
    # Report mode
    # ------------------------------------------------------------------
    def _print_report(self, single_schema: Optional[str] = None):
        qs = Tenant.objects.select_related("shard").exclude(schema_name="public")
        if single_schema:
            qs = qs.filter(schema_name=single_schema)

        self.stdout.write(
            f"{'SCHEMA':<20} {'SHARD':<14} {'STATUS':<13} "
            f"{'APPLIED':<10} {'PREV':<13} NOTES"
        )
        self.stdout.write("-" * 100)

        any_behind = any_pending = any_failed = False

        for t in qs:
            try:
                conn = connections[t.shard.alias]
            except ConnectionDoesNotExist:
                self.stdout.write(self.style.ERROR(
                    f"{t.schema_name:<20} {t.shard.alias:<14} {t.status:<13} "
                    f"{'?':<10} {t.previous_status:<13} shard alias not configured!"
                ))
                continue

            if not schema_exists(t.schema_name, t.shard.alias):
                applied_str = "-"
                note = (self.style.ERROR("ACTIVE but no schema!")
                        if t.status == Tenant.Status.ACTIVE else "no schema")
            else:
                applied = self._applied(conn, t.schema_name)
                expected = self._expected()
                applied_str = f"{applied}/{expected}"
                if applied >= expected:
                    note = (self.style.SUCCESS("up to date")
                            if t.status == Tenant.Status.ACTIVE else f"({t.status})")
                else:
                    if t.status == Tenant.Status.ACTIVE:
                        any_behind = True
                    note = self.style.WARNING(f"behind ({expected - applied} pending)")

            if t.status == Tenant.Status.PENDING:
                any_pending = True
            if t.status == Tenant.Status.FAILED:
                any_failed = True

            self.stdout.write(
                f"{t.schema_name:<20} {t.shard.alias:<14} {t.status:<13} "
                f"{applied_str:<10} {t.previous_status:<13} {note}"
            )

        self.stdout.write("")
        if any_behind:
            self.stdout.write(self.style.WARNING(
                "-> ACTIVE tenants are behind on migrations. "
                "Run `python manage.py migrate_schemas --tenant`."
            ))
        if any_pending:
            self.stdout.write(self.style.WARNING(
                "-> PENDING tenants detected. "
                "Run `python manage.py reconcile_tenants --only-pending`."
            ))
        if any_failed:
            self.stdout.write(self.style.ERROR(
                "-> FAILED tenants detected. Manual admin attention required."
            ))
        if not (any_behind or any_pending or any_failed):
            self.stdout.write(self.style.SUCCESS("-> All tenants in the expected state."))
