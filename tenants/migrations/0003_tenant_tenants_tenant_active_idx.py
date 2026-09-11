"""Partial covering index for the two reads Celery beat repeats forever.

commons.platform.tenancy.active_target_schemas / active_tenants_with_tz are deliberately
UNCACHED — the fanout dispatcher re-reads the ACTIVE-tenant set on every tick, which is what
lets sync_tenant_timezone use a plain .update() with nothing to invalidate. The cost of that
choice is this query running once per beat entry per tick, forever; the index is what keeps
it from being a sequential scan once the registry grows.

Partial on status='active' so it covers BOTH reads (conditioning on `timezone IS NOT NULL`
would serve only the calendar one), and covering (schema_name, timezone) so both are
index-only scans. See the comment on Tenant.Meta for why the condition is a literal.

Free to apply at any size: tenants_tenant is a registry table with near-zero write traffic,
so an extra index costs nothing on INSERT.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0002_seed_reserved_hosts'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='tenant',
            index=models.Index(condition=models.Q(('status', 'active')), fields=['schema_name', 'timezone'], name='tenants_tenant_active_idx'),
        ),
    ]
