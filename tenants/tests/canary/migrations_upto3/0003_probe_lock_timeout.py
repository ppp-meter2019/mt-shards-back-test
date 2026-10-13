from importlib import import_module

Migration = import_module("tenants.tests.canary.migrations.0003_probe_lock_timeout").Migration
