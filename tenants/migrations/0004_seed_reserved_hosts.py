"""Seed the initial reserved-host rules.

  * 10 GLOBAL subdomain-label rules (base_domain=""): a business tenant may never
    take a host whose leading label is one of these — on ANY domain, incl. its own
    custom domain (per the "all ten, globally" decision).
  * 2 EXACT apex rules for the platform base domains, so the bare apex itself
    cannot be claimed as a tenant host. Subdomains under them (e.g. acme.routegenie.com)
    remain available — only the apex is reserved.

Idempotent-ish: uses get_or_create keyed on the unique (match_type, value,
base_domain). Reverse removes exactly these seeded rows.
"""
from django.db import migrations

GLOBAL_LABELS = [
    "www", "api", "admin", "mail", "staging",
    "dev", "test", "status", "docs", "support",
]
APEX_DOMAINS = ["routegenie.com", "isi-technology.com"]


def seed(apps, schema_editor):
    ReservedHostRule = apps.get_model("tenants", "ReservedHostRule")
    for label in GLOBAL_LABELS:
        ReservedHostRule.objects.get_or_create(
            match_type="label", value=label, base_domain="",
            defaults={"is_active": True, "note": "Reserved service subdomain (seed)"},
        )
    for apex in APEX_DOMAINS:
        ReservedHostRule.objects.get_or_create(
            match_type="exact", value=apex, base_domain="",
            defaults={"is_active": True, "note": "Platform apex domain (seed)"},
        )


def unseed(apps, schema_editor):
    ReservedHostRule = apps.get_model("tenants", "ReservedHostRule")
    ReservedHostRule.objects.filter(
        match_type="label", value__in=GLOBAL_LABELS, base_domain="",
    ).delete()
    ReservedHostRule.objects.filter(
        match_type="exact", value__in=APEX_DOMAINS, base_domain="",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0003_reservedhostrule"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]