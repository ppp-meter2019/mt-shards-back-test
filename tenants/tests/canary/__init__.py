"""Canary tenant app for the migrator E2E tests (deploy/migrations_plan_rev7.md §20.1).
Installed ONLY by tenants/tests/settings_db_canary.py (and settings_db_standalone.py):
0002 runs CREATE INDEX CONCURRENTLY, which cannot run inside TestCase's transaction, so the
TestCase-based suites under settings_db must not see it.

  0001  atomic: canary_item (id, name), canary_probe (id, value)
  0002  atomic=False, the §8.2 recipe: CIC index on canary_item.name
  0003  RunPython: records current_setting('lock_timeout') in canary_probe
  0004  AddField canary_item.extra — a real ADD COLUMN (needs ACCESS EXCLUSIVE)

migrations_upto3/ re-exports 0001–0003 for LockTimeoutE2E (MIGRATION_MODULES).
"""
