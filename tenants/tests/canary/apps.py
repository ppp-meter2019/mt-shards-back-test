from django.apps import AppConfig


class CanaryConfig(AppConfig):
    name = "tenants.tests.canary"
    label = "canary"
    default_auto_field = "django.db.models.BigAutoField"
