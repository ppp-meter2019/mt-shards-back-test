from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="CanaryItem",
            fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                               verbose_name="ID")),
                    ("name", models.CharField(max_length=64))],
            options={"db_table": "canary_item"},
        ),
        migrations.CreateModel(
            name="CanaryProbe",
            fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False,
                                               verbose_name="ID")),
                    ("value", models.TextField())],
            options={"db_table": "canary_probe"},
        ),
    ]
