from importlib import import_module

Migration = import_module("tenants.tests.canary.migrations.0002_name_index_concurrently").Migration
