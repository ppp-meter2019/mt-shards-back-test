from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0002_shard_modified_alter_shard_created_on"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReservedHostRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("match_type", models.CharField(choices=[("exact", "Exact host"), ("suffix", "Domain suffix (host and all subdomains)"), ("label", "Subdomain label")], max_length=16)),
                ("value", models.CharField(max_length=253)),
                ("base_domain", models.CharField(blank=True, max_length=253)),
                ("is_active", models.BooleanField(default=True)),
                ("note", models.CharField(blank=True, max_length=200)),
                ("created_on", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["match_type", "value"],
            },
        ),
        migrations.AddConstraint(
            model_name="reservedhostrule",
            constraint=models.UniqueConstraint(
                fields=["match_type", "value", "base_domain"],
                name="tenants_reservedhostrule_unique",
            ),
        ),
    ]