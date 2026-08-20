"""Tenant-resolution cache invalidation — signal receivers.

These cover admin-driven .save()/.delete(). NOTE: the status machine mutates via
QuerySet.update() (migrate_schemas / reconcile_tenants / the deactivate-activate
API), which does NOT fire these signals — those call sites invalidate explicitly
via tenants.resolver.resolve_cache.forget_tenant(). See that module.
"""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Domain, Tenant
from .resolver import resolve_cache
from .resolver import host_registry


@receiver(post_save, sender=Domain)
def invalidate_domain_saved(sender, instance, **kwargs):
    # Clear any positive/negative entry so a new / re-pointed domain resolves immediately.
    # WARM stage: also SADD it to the host SET, arm the dead-man switch, and kick a
    # reconcile (all no-ops when WARM is off). Deferred to on_commit so a rolled-back
    # create never leaks into the SET.
    domain = instance.domain
    resolve_cache.forget_host(domain)

    def _apply():
        host_registry.add(domain)
        host_registry.arm()
        host_registry.trigger_warm()

    transaction.on_commit(_apply)


@receiver(post_delete, sender=Domain)
def invalidate_domain_deleted(sender, instance, **kwargs):
    # WARM stage: SREM from the host SET (host is gone → future misses reject), then
    # clear the positive so it isn't served as a HIT. Deferred to on_commit.
    domain = instance.domain
    resolve_cache.forget_host(domain)

    def _apply():
        host_registry.remove(domain)
        host_registry.arm()
        host_registry.trigger_warm()

    transaction.on_commit(_apply)


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