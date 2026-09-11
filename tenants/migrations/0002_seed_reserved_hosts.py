"""Seed the initial reserved-host rules.

Kept as a SEPARATE migration after the collapse of 0001-0009 rather than folded into
0001_initial: schema and data are different kinds of change, and only this one has to stay
idempotent (see below). Splitting them also keeps the router's public-schema data-migration
filter meaningful — it discriminates operations with no model, which is exactly this one.

  * 10 GLOBAL subdomain-label rules (base_domain=""): a business tenant may never
    take a host whose leading label is one of these — on ANY domain, incl. its own
    custom domain (per the "all ten, globally" decision).
  * 2 EXACT apex rules for the platform base domains, so the bare apex itself
    cannot be claimed as a tenant host. Subdomains under them (e.g. acme.routegenie.com)
    remain available — only the apex is reserved.

Idempotent-ish: uses get_or_create keyed on the unique (match_type, value,
base_domain). Reverse removes exactly these seeded rows.
"""
from typing import Any

from django.db import migrations

GLOBAL_LABELS = [
    "www", "api", "admin", "mail", "staging",
    "dev", "test", "status", "docs", "support",
]
APEX_DOMAINS = ["routegenie.com", "isi-technology.com"]


def seed(apps: Any, schema_editor: Any) -> None:
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


def unseed(apps: Any, schema_editor: Any) -> None:
    ReservedHostRule = apps.get_model("tenants", "ReservedHostRule")
    ReservedHostRule.objects.filter(
        match_type="label", value__in=GLOBAL_LABELS, base_domain="",
    ).delete()
    ReservedHostRule.objects.filter(
        match_type="exact", value__in=APEX_DOMAINS, base_domain="",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
