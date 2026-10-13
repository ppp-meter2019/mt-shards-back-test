from importlib import import_module

Migration = import_module("tenants.tests.canary.migrations.0001_initial").Migration
