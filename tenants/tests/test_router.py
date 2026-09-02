"""TenantDatabaseRouter strict guard: a business (shard-partitioned) model queried with NO
routing context (current_db unset) RAISES instead of silently routing to the default DB.
Quasi-shared contrib apps (contenttypes/auth/admin) and shared-only apps keep the benign
default. DB-free: db_for_read only inspects app membership + the current_db ContextVar."""
from django.test import SimpleTestCase

from tenants.context import use_alias
from tenants.routers import TenantDatabaseRouter


class RouterStrictGuardTests(SimpleTestCase):
    def setUp(self):
        self.r = TenantDatabaseRouter()

    def test_business_model_unset_context_raises(self):
        from products.models import Product
        with self.assertRaises(RuntimeError):
            self.r.db_for_read(Product)          # current_db unset (None) → strict raise

    def test_business_model_with_context_routes_to_shard(self):
        from products.models import Product
        with use_alias("shard_x"):
            self.assertEqual(self.r.db_for_read(Product), "shard_x")
            self.assertEqual(self.r.db_for_write(Product), "shard_x")

    def test_users_model_unset_context_raises(self):
        from users.models import User
        with self.assertRaises(RuntimeError):
            self.r.db_for_read(User)             # users is strict → contextless query raises

    def test_users_model_with_context_routes_to_shard(self):
        from users.models import User
        with use_alias("shard_x"):
            self.assertEqual(self.r.db_for_read(User), "shard_x")
            self.assertEqual(self.r.db_for_write(User), "shard_x")

    def test_contrib_tenant_model_unset_defaults_without_raise(self):
        from django.contrib.contenttypes.models import ContentType
        self.assertEqual(self.r.db_for_read(ContentType), "default")   # quasi-shared → benign

    def test_shared_only_model_is_default(self):
        from tenants.models import Tenant
        self.assertEqual(self.r.db_for_read(Tenant), "default")        # SHARED-only branch
