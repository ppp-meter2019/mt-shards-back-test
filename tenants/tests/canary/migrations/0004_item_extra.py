from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("canary", "0003_probe_lock_timeout")]
    operations = [
        migrations.AddField("canaryitem", "extra", models.CharField(max_length=64, null=True)),
    ]
