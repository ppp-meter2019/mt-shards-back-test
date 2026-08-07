"""Tenant-resolution cache invalidation — signal receivers.

These cover admin-driven .save()/.delete(). NOTE: the status machine mutates via
QuerySet.update() (migrate_schemas / reconcile_tenants / the deactivate-activate
API), which does NOT fire these signals — those call sites invalidate explicitly
via tenants.resolve_cache.resolve_cache.forget_tenant(). See that module.
"""
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


@receiver(post_save, sender=Tenant)
def invalidate_tenant(sender, instance, **kwargs):
    # Status/shard change via .save() => drop every one of the tenant's domains.
    resolve_cache.forget_tenant(instance)