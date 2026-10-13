"""Upstream-contract canaries for the pinned django-tenants (see deploy/UPSTREAM_FORK.md).

These do NOT test our behaviour. They assert that the pinned upstream still exposes what the
fork points depend on. Testing a third-party library is normally an antipattern; it is
justified here by two facts that `requirements.txt` cannot express:

  * we COPIED upstream code (the body of `allow_migrate`, `run_from_argv`), so its
    correctness is now partly ours;
  * we reach into PRIVATE surfaces (`parser._actions`, `SyncCommon.handle` called unbound
    past the MRO, `_notice`), which carry no compatibility guarantee at all.

Two of the five below are the mirror image: they assert that OUR patch still lands on
upstream (the stripped `--database` default; the `apps.ready()` monkeypatch). Same purpose —
the coupling is invisible everywhere else.

LIMIT, stated plainly: these check SHAPE, not semantics. If upstream fixes a bug INSIDE a
body we copied, every assertion here still passes while our copy keeps the old behaviour.
That half of the risk is covered only by the manual diff in UPSTREAM_FORK.md §7, step 2-3.

A failure here means: read deploy/UPSTREAM_FORK.md before changing anything.

MT-only by placement: `tenants/tests/` is not collected in standalone, where django_tenants
is not installed at all.
"""
import argparse
import inspect
import io
import sys
from unittest import mock

from django.test import SimpleTestCase

import django_tenants.utils as dt_utils
from django_tenants.management.commands import SyncCommon
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.routers import TenantSyncRouter

from tenants.management.commands.migrate_schemas import Command as MigrateSchemasOverride


class UpstreamShapeTests(SimpleTestCase):
    """Symbols and signatures the fork points call into."""

    def test_synccommon_handle_signature(self) -> None:
        """migrate_schemas.handle() calls SyncCommon.handle(self, ...) UNBOUND, deliberately
        bypassing the MRO so the upstream flag parsing runs before our own. A changed
        signature would be a TypeError at best and a silent behaviour shift at worst."""
        self.assertEqual(str(inspect.signature(SyncCommon.handle)), "(self, *args, **options)")

    def test_synccommon_notice_exists(self) -> None:
        """Every progress line our migrate_schemas prints goes through _notice()."""
        self.assertTrue(callable(getattr(SyncCommon, "_notice", None)))

    def test_router_app_in_list_exists(self) -> None:
        """Called from the allow_migrate body we copied into tenants/routers.py."""
        self.assertTrue(callable(getattr(TenantSyncRouter, "app_in_list", None)))

    def test_hostname_from_request_still_strips_www(self) -> None:
        """The resolve-cache key, treg:hosts membership and the Domain lookup are ALL built
        from the hostname this returns. If the www-stripping ever changed, cache keys and SET
        members would silently stop matching incoming Hosts."""
        self.assertTrue(callable(getattr(TenantMainMiddleware, "hostname_from_request", None)))
        self.assertEqual(dt_utils.remove_www("www.x.com"), "x.com")
        self.assertEqual(dt_utils.remove_www("x.com"), "x.com")


class OurPatchLandsTests(SimpleTestCase):
    """The two places where OUR code modifies upstream state at import/ready time."""

    def test_database_option_default_is_stripped(self) -> None:
        """migrate_schemas.add_arguments walks parser._actions (a private argparse surface)
        to turn the upstream default of '--database=default' into None, so "not specified" is
        distinguishable from "explicitly default". If that default came back, a no-flag full
        run would read 'default' in the tenant branch and silently skip EVERY shard."""
        parser = argparse.ArgumentParser()
        MigrateSchemasOverride().add_arguments(parser)
        database = [a for a in parser._actions if a.dest == "database"]
        self.assertEqual(len(database), 1, "upstream stopped declaring --database")
        self.assertIsNone(database[0].default)

    def test_context_monkeypatch_target_and_effect(self) -> None:
        """tenants.apps.ready() rebinds `dt_utils.schema_context` / `dt_utils.tenant_context`
        to our shard-aware versions, as a safety net for LATE third-party importers. If the
        names moved, the patch would become a silent no-op — and such importers would keep the
        single-DB originals, routing to the wrong shard.

        NB the aliased spelling above is deliberate: scripts/ci_guard_schema_name.sh's sibling
        guard forbids the full dotted path anywhere but apps.py, and it greps prose too."""
        for name in ("schema_context", "tenant_context"):
            with self.subTest(name=name):
                patched = getattr(dt_utils, name, None)
                self.assertIsNotNone(patched, f"django_tenants.utils.{name} is gone")
                self.assertEqual(patched.__module__, "tenants.context",
                                 "apps.ready() monkeypatch did not take effect")


class MigratorUpstreamContractTests(SimpleTestCase):
    """What tenants.migrator / migrate_schemas assume about django-tenants, psycopg, the
    stdlib and the Django admin (deploy/migrations_plan_rev7.md §18.2; UPSTREAM_FORK.md §4).
    run_migrations is driven for real here, with its DB edges mocked: the canaries check the
    BEHAVIOUR the migrator compensates for, not just that the names exist."""

    class _Tty(io.StringIO):
        def isatty(self) -> bool:                        # upstream styles only a terminal
            return True

    def _run(self, *, fail: bool = False, idx: int | None = None, count: int | None = None):
        from django_tenants.migration_executors import base

        class FakeMigrate:
            def __init__(self, stdout, stderr) -> None:
                self.stdout = stdout

            def execute(self, *args, **options) -> None:
                self.stdout.write("  Applying app.0001_x... OK")
                if fail:
                    raise ValueError("boom")

        conn = mock.MagicMock()
        out = self._Tty()
        with mock.patch("django.db.connections", {"tenant_1": conn}), \
             mock.patch.object(base, "MigrationRecorder"), \
             mock.patch.object(base, "transaction"), \
             mock.patch.object(base, "get_tenant_base_migrate_command_class", return_value=FakeMigrate), \
             mock.patch.object(sys, "stdout", out):
            try:
                base.run_migrations([], {"database": "tenant_1", "verbosity": 1}, "standard",
                                    "acme", idx=idx, count=count)
            except ValueError:
                pass
        return conn, out.getvalue().splitlines()

    def test_run_migrations_line_prefix_is_what_the_log_tee_strips(self) -> None:
        """§9.9: the per-tenant log file holds the output WITHOUT upstream's
        "[standard:acme] " prefix (coloured on a terminal, "[1/9 (11%) …]" with progress)."""
        from tenants.migrator import _clean
        for kw in ({}, {"idx": 0, "count": 9}):
            _, lines = self._run(**kw)
            applied = [line for line in lines if "Applying" in line]
            self.assertEqual(len(applied), 1, lines)
            self.assertNotEqual(applied[0], "  Applying app.0001_x... OK")   # really prefixed
            self.assertEqual(_clean(applied[0]), "  Applying app.0001_x... OK", applied[0])

    def test_run_migrations_closes_the_connection_only_on_success(self) -> None:
        """§7: hence _close_shard in the migrator's finally — on a failure upstream leaves the
        shard connection open, with our lock_timeout on it."""
        conn, _ = self._run()
        conn.close.assert_called_once_with()
        conn, _ = self._run(fail=True)
        conn.close.assert_not_called()

    def test_set_schema_takes_include_public(self) -> None:
        """B1 (§5.4) and the trap at base.py:47-49: pending_plan() reads the tenant's own
        django_migrations with set_schema(..., include_public=False)."""
        from django_tenants.postgresql_backend.base import DatabaseWrapper
        self.assertIn("include_public", inspect.signature(DatabaseWrapper.set_schema).parameters)

    def test_psycopg_cancels_and_waits_on_keyboard_interrupt(self) -> None:
        """§6.5: after Ctrl-C the lease is released only if the shard backend is idle — which
        relies on psycopg cancelling the running query and waiting for it before re-raising,
        and on transaction_status telling ACTIVE from idle."""
        from psycopg import connection as pg_connection
        interrupted = pg_connection._INTERRUPTED                 # a class or a tuple of them
        self.assertTrue(issubclass(KeyboardInterrupt, interrupted), interrupted)
        src = inspect.getsource(pg_connection.Connection.wait)
        self.assertIn("_try_cancel", src)
        self.assertIn("transaction_status == ACTIVE", src)

    def test_multiprocessing_and_prctl(self) -> None:
        """§9.7 m7: the hard stop kills multiprocessing.active_children(); §9.6: workers
        arm PR_SET_PDEATHSIG through libc's prctl (Linux)."""
        import ctypes
        import multiprocessing
        self.assertTrue(callable(multiprocessing.active_children))
        self.assertTrue(hasattr(ctypes.CDLL(None), "prctl"))

    def test_admin_changeform_view_runs_in_a_transaction(self) -> None:
        """§14.2: TenantAdmin.save_model rolls a lost status CAS back with
        transaction.set_rollback(True), which needs the admin's surrounding atomic()."""
        from django.contrib.admin import ModelAdmin
        self.assertIn("transaction.atomic", inspect.getsource(ModelAdmin.changeform_view))
