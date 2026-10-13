"""Records the session's lock_timeout: after 0002's LockTimeoutRestore it must be the
migrator's value again ('5s' in MT), or nothing at all in standalone."""
from django.db import migrations


def probe(apps, schema_editor):
    with schema_editor.connection.cursor() as c:
        c.execute("INSERT INTO canary_probe (value) SELECT current_setting('lock_timeout')")


class Migration(migrations.Migration):
    dependencies = [("canary", "0002_name_index_concurrently")]
    operations = [migrations.RunPython(probe, migrations.RunPython.noop)]
