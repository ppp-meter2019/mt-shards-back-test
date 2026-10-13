"""The §8.2 recipe, verbatim in shape: what every real CIC migration must look like."""
from django.db import migrations, models

from commons.platform.migration_ops import LockTimeoutOff, LockTimeoutRestore


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("canary", "0001_initial")]
    operations = [
        LockTimeoutOff(),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AddIndex(
                "canaryitem", models.Index(fields=["name"], name="canary_item_name_0002_idx"))],
            database_operations=[
                migrations.RunSQL('DROP INDEX CONCURRENTLY IF EXISTS "canary_item_name_0002_idx"',
                                  migrations.RunSQL.noop),
                migrations.RunSQL('CREATE INDEX CONCURRENTLY "canary_item_name_0002_idx" '
                                  'ON "canary_item" ("name")',
                                  'DROP INDEX CONCURRENTLY IF EXISTS "canary_item_name_0002_idx"'),
            ]),
        LockTimeoutRestore(),
    ]
