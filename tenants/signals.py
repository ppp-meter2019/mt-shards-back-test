"""Tenant-resolution cache invalidation — signal receivers.

These cover admin-driven .save()/.delete(). NOTE: the status machine mutates via
QuerySet.update() (migrate_schemas / reconcile_tenants / the deactivate-activate
API), which does NOT fire these signals — those call sites invalidate explicitly
via tenants.resolve_cache.resolve_cache.forget_tenant(). See that module.
"""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Domain, Tenant
from .resolve_cache import resolve_cache


@receiver(post_save, sender=Domain)
@receiver(post_delete, sender=Domain)
def invalidate_domain(sender, instance, **kwargs):
    # Clears both a positive entry and any negative (miss) entry for this host,
    # so a newly created / re-pointed domain resolves immediately.
    resolve_cache.forget_host(instance.domain)


def _bump_beat(schema_name):
    from .celery.change_marker import bump_schema
    transaction.on_commit(lambda: bump_schema(schema_name))


@receiver(post_save, sender=Tenant)
def invalidate_tenant(sender, instance, created, **kwargs):
    # Always drop the tenant's cached resolve snapshots (cheap, any save).
    resolve_cache.forget_tenant(instance)
    # Nudge beat ONLY on an actual STATUS change via a .save() path (admin, shell, a
    # command using obj.save()). `created` => new tenant (NEW, not schedulable) → skip.
    # A company_name/description edit leaves status unchanged → skip (a full beat reload
    # is expensive). `_loaded_status` is stamped by Tenant.from_db.
    # NOTE: QuerySet.update() bypasses signals entirely — those code paths bump
    # explicitly (migrate_schemas / reconcile / the activate-deactivate API); a raw
    # manual `.update(status=...)` in a shell is recovered with `resync_beat_schedules`.
    prev = getattr(instance, "_loaded_status", None)
    if not created and prev is not None and prev != instance.status:
        _bump_beat(instance.schema_name)
    instance._loaded_status = instance.status   # so a later save() doesn't re-bump


@receiver(post_delete, sender=Tenant)
def beat_forget_deleted_tenant(sender, instance, **kwargs):
    # Tenant removed (admin / shell / API delete, all via .delete()). Only an ACTIVE
    # tenant was in beat's schedule, so only then does beat need to reload; deleting a
    # NEW/PENDING/DEACTIVATED/FAILED one changes nothing there → skip the costly reload.
    if instance.status == Tenant.Status.ACTIVE:
        _bump_beat(instance.schema_name)