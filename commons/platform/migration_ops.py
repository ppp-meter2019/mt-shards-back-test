"""Migration operations for non-transactional (``atomic = False``) migrations
(deploy/migrations_plan_rev7.md §8.2). Mode-independent: business migrations run under
the multi-tenant migrator AND under plain Django `migrate` in standalone, so this module
imports nothing from `tenants`.

The migrator arms every tenant's shard connection with ``lock_timeout`` (5s in MT) before
running its migrations. That is right for ordinary DDL, which must not queue behind the
application's traffic for long, but wrong for ``CREATE INDEX CONCURRENTLY``: CIC waits for
every transaction older than itself by design and would fail its timeout. The recipe:

    class Migration(migrations.Migration):
        atomic = False
        operations = [
            LockTimeoutOff(),
            migrations.SeparateDatabaseAndState(
                state_operations=[migrations.AddIndex("model", models.Index(
                    fields=["f"], name="app_model_f_0042_idx"))],
                database_operations=[
                    migrations.RunSQL('DROP INDEX CONCURRENTLY IF EXISTS "app_model_f_0042_idx"',
                                      migrations.RunSQL.noop),
                    migrations.RunSQL('CREATE INDEX CONCURRENTLY "app_model_f_0042_idx" '
                                      'ON "app_model" ("f")',
                                      'DROP INDEX CONCURRENTLY IF EXISTS "app_model_f_0042_idx"'),
                ]),
            LockTimeoutRestore(),
        ]

Enforced by the CI lint commons/tests/test_migration_lint.py:
  * every ``atomic = False`` migration (also ``atomic: bool = False``) starts with
    LockTimeoutOff() and ends with LockTimeoutRestore();
  * every ``CREATE [UNIQUE] INDEX CONCURRENTLY "<name>"`` is preceded in the same migration
    by ``DROP INDEX CONCURRENTLY IF EXISTS "<name>"`` — that is what makes a retry after a
    failed, INVALID index safe;
  * no ``CONCURRENTLY IF NOT EXISTS`` (an INVALID leftover would count as "existing" and be
    kept for good) and no AddIndexConcurrently.
NOT checked automatically — the author's job: one unique index name per migration, and
SQL built at runtime (f-strings, variables) is invisible to the lint.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import migrations

_PATH = "commons.platform.migration_ops"


class LockTimeoutOff(migrations.RunSQL):
    """``SET lock_timeout = 0`` for the rest of this (non-atomic) migration's session."""

    def __init__(self) -> None:
        super().__init__(sql="SET lock_timeout = 0", reverse_sql=migrations.RunSQL.noop)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return f"{_PATH}.LockTimeoutOff", [], {}


def _restore(apps: Any, schema_editor: Any) -> None:
    """Back to the migrator's value (MT) or the server default (standalone: None)."""
    v = getattr(settings, "TENANT_MIGRATE_DDL_LOCK_TIMEOUT", None)
    with schema_editor.connection.cursor() as c:
        if v is None:
            c.execute("SET lock_timeout TO DEFAULT")
        else:
            c.execute("SELECT set_config('lock_timeout', %s, false)", [v])


class LockTimeoutRestore(migrations.RunPython):
    """Undo LockTimeoutOff: the next migration of this run must be bounded again."""

    def __init__(self) -> None:
        super().__init__(_restore, migrations.RunPython.noop)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return f"{_PATH}.LockTimeoutRestore", [], {}
