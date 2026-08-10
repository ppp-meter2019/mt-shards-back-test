from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0004_seed_reserved_hosts"),
    ]

    operations = [
        # Rename preserves data AND the unique index (column is just renamed).
        migrations.RenameField(
            model_name="tenant",
            old_name="name",
            new_name="company_name",
        ),
        migrations.AddField(
            model_name="tenant",
            name="description",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,   # backfill existing rows with "", no model default
        ),
    ]
