"""Domain.domain becomes CANONICAL by construction: lower(rtrim(trim(domain), '.')).

Domain.save() already normalizes, but bulk_create() / QuerySet.update() / raw SQL bypass it,
and a non-canonical row is not merely untidy — request.get_host() returns the raw header and
this column compares exactly, so such a tenant is unreachable. The constraint also earns
ReservedHostRule.candidate_q() the right to compare the column DIRECTLY (it previously had
to wrap it in RTRIM+UPPER to stay a superset of matches(), which cost the unique index).

Safe to apply on an empty/greenfield estate. A future import of the legacy single-tenant
estate MUST normalize its hostnames before insert — that is the point: the insert fails
loudly instead of producing tenants nobody can reach.
"""

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0008_taskrun_args_sig'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='domain',
            constraint=models.CheckConstraint(condition=models.Q(('domain', models.Func(django.db.models.functions.text.Lower(django.db.models.functions.text.Trim('domain')), models.Value('.'), function='RTRIM'))), name='tenants_domain_canonical'),
        ),
    ]
