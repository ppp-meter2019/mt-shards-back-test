from django.db import models


class CanaryItem(models.Model):
    name = models.CharField(max_length=64)
    extra = models.CharField(max_length=64, null=True)

    class Meta:
        db_table = "canary_item"
        indexes = [models.Index(fields=["name"], name="canary_item_name_0002_idx")]


class CanaryProbe(models.Model):
    value = models.TextField()

    class Meta:
        db_table = "canary_probe"
