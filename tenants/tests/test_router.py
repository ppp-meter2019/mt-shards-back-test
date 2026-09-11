"""TenantDatabaseRouter: the strict routing guard and the public data-migration filter.

Strict guard — a business (shard-partitioned) model queried with NO routing context
(current_db unset) RAISES instead of silently routing to the default DB. Quasi-shared
contrib apps (contenttypes/auth/admin) and shared-only apps keep the benign default.

Public-schema guard — a tenant-model QUERY whose target connection sits on public is
refused, because the closure apps' tables are there but EMPTY and would answer with a
plausible-looking zero rows.

Data-migration filter — a closure app's RunPython/RunSQL must not run on the public schema
(see the comment in allow_migrate and settings_base._PUBLIC_MODEL_ALLOWLIST).

DB-free throughout: db_for_read inspects app membership + the ContextVar, and allow_migrate
reads connection.schema_name, which django-tenants sets without opening a connection.
"""
from types import SimpleNamespace
from unittest import mock

from django.db import connections
from django.test import SimpleTestCase, override_settings

from tenants.context import use_alias
from tenants.routers import PublicSchemaModelDenied, TenantDatabaseRouter


class RouterStrictGuardTests(SimpleTestCase):
    def setUp(self) -> None:
        self.r = TenantDatabaseRouter()

    def test_business_model_unset_context_raises(self) -> None:
        """Isolated from the public guard on purpose: with `default` sitting on public — the
        usual state — a contextless business query is refused by _guard_public instead, and
        rightly so (there is no shard to bind). Moving the connection onto a tenant schema
        leaves exactly the condition this test is about: the schema is known, the SHARD is
        not."""
        from products.models import Product
        with mock.patch.object(connections["default"], "schema_name", "alpha"):
            with self.assertRaises(RuntimeError) as caught:
                self.r.db_for_read(Product)      # current_db unset (None) → strict raise
        self.assertNotIsInstance(caught.exception, PublicSchemaModelDenied)

    def test_business_model_with_context_routes_to_shard(self) -> None:
        from products.models import Product
        with use_alias("shard_x"):
            self.assertEqual(self.r.db_for_read(Product), "shard_x")
            self.assertEqual(self.r.db_for_write(Product), "shard_x")

    def test_users_model_unset_context_raises(self) -> None:
        """Needs no such isolation: the identity model IS allowlisted on public, so the
        public guard steps aside and the strict one is what speaks. Superuser creation is
        expected to go through `tenant_command <cmd> --schema=public`, which establishes a
        context — a bare contextless User query stays a bug on either schema."""
        from users.models import User
        with self.assertRaises(RuntimeError):
            self.r.db_for_read(User)             # users is strict → contextless query raises

    def test_users_model_with_context_routes_to_shard(self) -> None:
        from users.models import User
        with use_alias("shard_x"):
            self.assertEqual(self.r.db_for_read(User), "shard_x")
            self.assertEqual(self.r.db_for_write(User), "shard_x")

    def test_contrib_tenant_model_unset_defaults_without_raise(self) -> None:
        from django.contrib.contenttypes.models import ContentType
        self.assertEqual(self.r.db_for_read(ContentType), "default")   # quasi-shared → benign

    def test_shared_only_model_is_default(self) -> None:
        from tenants.models import Tenant
        self.assertEqual(self.r.db_for_read(Tenant), "default")        # SHARED-only branch


class PublicDataMigrationFilterTests(SimpleTestCase):
    """Runs against the REAL configuration — no override_settings. `orders` is a per-tenant
    app, so it is already in SHARED_APPS (every one of them is, so that the identity table's
    FK targets exist in public) and already in TENANT_STRICT_ROUTE_APPS, which is what the
    filter keys on. A synthetic app list would have tested the wiring instead of the wiring
    that ships."""

    def setUp(self) -> None:
        self.r = TenantDatabaseRouter()

    def test_data_migration_is_skipped_on_public(self) -> None:
        """RunPython / RunSQL reach a router with NO model at all
        (django/db/migrations/operations/special.py) — that absence is the only signal, and
        it is what this filter keys on."""
        self.assertIs(self.r.allow_migrate("default", "orders"), False)

    def test_schema_operation_still_runs_on_public(self) -> None:
        """CreateModel & co. arrive via allow_migrate_model(), which always passes the model.
        They MUST still run: creating those tables in public is the entire reason the app is
        in SHARED_APPS."""
        self.assertIs(self.r.allow_migrate("default", "orders", model_name="order"), True)

    def test_app_outside_the_list_is_untouched(self) -> None:
        """`tenants` is shared but NOT a closure app, so its own data migrations (the
        reserved-host seed, 0004) must keep running on public."""
        self.assertIs(self.r.allow_migrate("default", "tenants"), True)

    def test_data_migration_runs_on_a_shard_in_a_tenant_schema(self) -> None:
        """The positive control the rest of this class lacks: blocking on public is only
        correct if the SAME migration still runs where the data belongs.

        A shard alias cannot be conjured with override_settings — Django does not repopulate
        the connection thread-local for an alias added mid-test — so the connection registry
        the router reads is replaced outright. That is also the honest shape of a real
        `migrate_schemas --tenant`: tenant schemas live on non-default aliases.
        """
        fake = {"shard_a": SimpleNamespace(schema_name="alpha")}
        with mock.patch("tenants.routers.connections", fake):
            self.assertIs(self.r.allow_migrate("shard_a", "orders"), True)
            self.assertIs(self.r.allow_migrate("shard_a", "orders", model_name="order"), True)

    def test_filter_is_inert_off_the_public_schema(self) -> None:
        """Off public the two verdicts must be IDENTICAL — that equality is what proves the
        model_name gate never fired, since on public they differ (False vs True, above).
        Patching the public-schema NAME is what makes the connection's own "public" a tenant
        schema, without needing a second alias or a live connection."""
        with mock.patch("tenants.routers.get_public_schema_name",
                        return_value="__not_public__"):
            data_migration = self.r.allow_migrate("default", "orders")
            schema_operation = self.r.allow_migrate("default", "orders", model_name="order")
        self.assertEqual(data_migration, schema_operation)


class PublicSchemaQueryGuardTests(SimpleTestCase):
    """`use_alias("default")` reproduces the management host exactly: that is what
    TenantShardRoutingMiddleware pins for a request with no tenant, and `default` sits on the
    public schema. `products` stands in for any business app, `users` for the identity app
    whose rows public legitimately holds."""

    def setUp(self) -> None:
        self.r = TenantDatabaseRouter()

    def test_business_model_on_public_is_refused(self) -> None:
        """The case the guard exists for. Without it the ORM answers with zero rows from an
        empty (or absent) table — a wrong answer that looks like a right one."""
        from products.models import Product
        with use_alias("default"), self.assertRaises(PublicSchemaModelDenied):
            self.r.db_for_read(Product)

    def test_write_is_guarded_too(self) -> None:
        """db_for_write delegates to db_for_read, so the refusal covers INSERT/UPDATE as
        well — worth pinning, since a write into an empty public table is the worse half."""
        from products.models import Product
        with use_alias("default"), self.assertRaises(PublicSchemaModelDenied):
            self.r.db_for_write(Product)

    def test_allowlisted_model_on_public_is_allowed(self) -> None:
        """The operator's identity IS a row in public — refusing it would break the very
        login the public admin exists for."""
        from users.models import User
        with use_alias("default"):
            self.assertEqual(self.r.db_for_read(User), "default")

    def test_quasi_shared_contrib_model_is_out_of_scope(self) -> None:
        """contenttypes / auth / admin are tenant apps too, but Django queries them on public
        legitimately. They are outside TENANT_STRICT_ROUTE_APPS by design, and the guard
        reuses that boundary rather than drawing a second one."""
        from django.contrib.contenttypes.models import ContentType
        with use_alias("default"):
            self.assertEqual(self.r.db_for_read(ContentType), "default")

    def test_guard_is_schema_gated_not_alias_gated(self) -> None:
        """Same alias, same model — only the schema differs. Patching the public-schema NAME
        makes this connection's own "public" a tenant schema without needing a live second
        alias, and the refusal must disappear."""
        from products.models import Product
        with mock.patch("tenants.routers.get_public_schema_name",
                        return_value="__not_public__"):
            with use_alias("default"):
                self.assertEqual(self.r.db_for_read(Product), "default")

    def test_unknown_alias_does_not_crash_the_router(self) -> None:
        """An alias with no connection fails at execute anyway; the guard must not replace
        that error with a ConnectionDoesNotExist raised from inside the ROUTER, which would
        be far harder to read."""
        from products.models import Product
        with use_alias("no_such_shard"):
            self.assertEqual(self.r.db_for_read(Product), "no_such_shard")

    @override_settings(PUBLIC_MODEL_GUARD="warn")
    def test_warn_mode_logs_and_continues(self) -> None:
        """The merge MEASUREMENT mode: the allowlist is discovered by reading this log, so it
        must name the model in the form the allowlist takes."""
        from products.models import Product
        with use_alias("default"):
            with self.assertLogs("tenants.routers", level="WARNING") as captured:
                self.assertEqual(self.r.db_for_read(Product), "default")
        self.assertIn("products.product", "\n".join(captured.output))

    @override_settings(PUBLIC_MODEL_GUARD="off")
    def test_off_mode_skips_the_check(self) -> None:
        from products.models import Product
        with use_alias("default"):
            self.assertEqual(self.r.db_for_read(Product), "default")

    def test_contextless_query_on_public_names_the_schema_not_the_shard(self) -> None:
        """Both refusals are true here, and only one is useful: on public there is no shard
        to bind, so the strict guard's "establish a routing context" would send the reader
        hunting a bug that does not exist. The specific diagnosis has to win."""
        from products.models import Product
        with self.assertRaises(PublicSchemaModelDenied):
            self.r.db_for_read(Product)          # no use_alias() at all

    def test_contextless_query_off_public_still_names_the_missing_context(self) -> None:
        """The mirror image, and what stops the reordering from swallowing the strict guard:
        move `default` onto a tenant schema — which is what django-tenants does for the whole
        of a tenant request — and the missing shard is once again the real fault."""
        from products.models import Product
        with mock.patch.object(connections["default"], "schema_name", "alpha"):
            with self.assertRaises(RuntimeError) as caught:
                self.r.db_for_read(Product)
        self.assertNotIsInstance(caught.exception, PublicSchemaModelDenied)
