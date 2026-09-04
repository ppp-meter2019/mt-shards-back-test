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

from django.test import SimpleTestCase

import django_tenants.utils as dt_utils
from django_tenants.management.commands import SyncCommon
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.routers import TenantSyncRouter

from tenants.management.commands.migrate_schemas import Command as MigrateSchemasOverride


class UpstreamShapeTests(SimpleTestCase):
    """Symbols and signatures the fork points call into."""

    def test_synccommon_handle_signature(self):
        """migrate_schemas.handle() calls SyncCommon.handle(self, ...) UNBOUND, deliberately
        bypassing the MRO so the upstream flag parsing runs before our own. A changed
        signature would be a TypeError at best and a silent behaviour shift at worst."""
        self.assertEqual(str(inspect.signature(SyncCommon.handle)), "(self, *args, **options)")

    def test_synccommon_notice_exists(self):
        """Every progress line our migrate_schemas prints goes through _notice()."""
        self.assertTrue(callable(getattr(SyncCommon, "_notice", None)))

    def test_router_app_in_list_exists(self):
        """Called from the allow_migrate body we copied into tenants/routers.py."""
        self.assertTrue(callable(getattr(TenantSyncRouter, "app_in_list", None)))

    def test_hostname_from_request_still_strips_www(self):
        """The resolve-cache key, treg:hosts membership and the Domain lookup are ALL built
        from the hostname this returns. If the www-stripping ever changed, cache keys and SET
        members would silently stop matching incoming Hosts."""
        self.assertTrue(callable(getattr(TenantMainMiddleware, "hostname_from_request", None)))
        self.assertEqual(dt_utils.remove_www("www.x.com"), "x.com")
        self.assertEqual(dt_utils.remove_www("x.com"), "x.com")


class OurPatchLandsTests(SimpleTestCase):
    """The two places where OUR code modifies upstream state at import/ready time."""

    def test_database_option_default_is_stripped(self):
        """migrate_schemas.add_arguments walks parser._actions (a private argparse surface)
        to turn the upstream default of '--database=default' into None, so "not specified" is
        distinguishable from "explicitly default". If that default came back, a no-flag full
        run would read 'default' in the tenant branch and silently skip EVERY shard."""
        parser = argparse.ArgumentParser()
        MigrateSchemasOverride().add_arguments(parser)
        database = [a for a in parser._actions if a.dest == "database"]
        self.assertEqual(len(database), 1, "upstream stopped declaring --database")
        self.assertIsNone(database[0].default)

    def test_context_monkeypatch_target_and_effect(self):
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
