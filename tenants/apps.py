from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenants"

    def ready(self) -> None:
        # Safety net for THIRD-PARTY / django_tenants code that pulls the context helpers out of
        # django_tenants.utils (e.g. django_tenants' own `collectstatic_schemas` command) — such
        # LATE importers (command modules load after ready()) then get our shard-aware versions
        # instead of the single-DB originals.
        #
        # PARTIAL by nature: a module that imported those helpers BEFORE ready() keeps the
        # original. That is why PROJECT code must import them from `tenants.context`, never rely
        # on this patch — enforced statically by scripts/ci_guard_context_import.sh. (This aliased
        # assignment form is invisible to that guard, which is correct: this is the sole patch site.)
        import django_tenants.utils as dt_utils

        from .context import schema_context, tenant_context

        dt_utils.schema_context = schema_context
        dt_utils.tenant_context = tenant_context

        # Register tenant-resolution cache invalidation signals.
        from . import signals  # noqa: F401

        # Register deploy-time config checks (gate/warm flag invariants).
        from . import checks  # noqa: F401
