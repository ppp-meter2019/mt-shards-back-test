"""Add TaskRun.args_sig and widen the uniqueness key to (schema, task, args_sig).

Two calendar schedule entries may share a task NAME and differ only by args. The fanout
overlap-lock already discriminates on that axis; the watermark did not, so the entry whose
wave landed second read the first one's watermark, saw the occurrence as already run, and
was skipped indefinitely. See tenants.models.TaskRun and deploy/celery_fanout_design.md §3.

Existing rows backfill to "", which is exactly what tenants.celery.dispatch.argsig returns
for an entry with no args — the overwhelming majority. So the backfill is not a guess: those
rows keep the identity they already had and keep being honoured, and the new column only
ever carries a value for an entry that actually passes args.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0007_alter_tenant_status_changed_at"),
    ]

    operations = [
        # Drop first: the new column participates in the replacement constraint, and the
        # old one still covers (schema, task) while the column is being added.
        migrations.RemoveConstraint(
            model_name="taskrun",
            name="tenants_taskrun_unique",
        ),
        migrations.AddField(
            model_name="taskrun",
            name="args_sig",
            field=models.CharField(blank=True, default="", max_length=12),
        ),
        migrations.AddConstraint(
            model_name="taskrun",
            constraint=models.UniqueConstraint(
                fields=("schema", "task", "args_sig"),
                name="tenants_taskrun_unique",
            ),
        ),
    ]
