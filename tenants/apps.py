from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenants"

    def ready(self):
        # Safety net: make late importers of the django-tenants context helpers
        # get our shard-aware versions (which wire current_db + the schema on
        # the SHARD connection, not just the default one).
        #
        # NOTE: `from django_tenants.utils import schema_context` binds at
        # import time - modules imported BEFORE ready() keep the original.
        # That's why project code must import from `tenants.context` directly
        # (the import rule); this patch only covers third-party/legacy paths
        # that import after startup.
        import django_tenants.utils as dt_utils

        from .context import schema_context, tenant_context

        dt_utils.schema_context = schema_context
        dt_utils.tenant_context = tenant_context
