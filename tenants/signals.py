"""Tenant-resolution cache invalidation — signal receivers.

These cover admin-driven .save()/.delete(). NOTE: the status machine mutates via
QuerySet.update() (migrate_schemas / reconcile_tenants / the deactivate-activate
API), which does NOT fire these signals — those call sites invalidate explicitly
via tenants.resolver.resolve_cache.forget_tenant(). See that module.
"""
from typing import Any

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Domain, Tenant
from .resolver import resolve_cache
from .resolver import host_registry


@receiver(post_save, sender=Domain)
def invalidate_domain_saved(sender: type[Domain], instance: Domain, **kwargs: Any) -> None:
    # Clear any positive/negative entry so a new / re-pointed domain resolves immediately.
    # WARM stage: also SADD it to the host SET, arm the dead-man switch, and kick a
    # reconcile (all no-ops when WARM is off). Deferred to on_commit so a rolled-back
    # create never leaks into the SET.
    domain = instance.domain
    resolve_cache.forget_host(domain)

    def _apply() -> None:
        host_registry.add(domain)
        host_registry.arm()
        host_registry.trigger_warm()

    transaction.on_commit(_apply)


@receiver(post_delete, sender=Domain)
def invalidate_domain_deleted(sender: type[Domain], instance: Domain, **kwargs: Any) -> None:
    # WARM stage: SREM from the host SET (host is gone → future misses reject), then
    # clear the positive so it isn't served as a HIT. Deferred to on_commit.
    domain = instance.domain
    resolve_cache.forget_host(domain)

    def _apply() -> None:
        host_registry.remove(domain)
        host_registry.arm()
        host_registry.trigger_warm()

    transaction.on_commit(_apply)


@receiver(post_save, sender=Tenant)
def invalidate_tenant(sender: type[Tenant], instance: Tenant, **kwargs: Any) -> None:
    # Drop the tenant's cached resolve snapshots on any save (cheap). No beat nudge:
    # the fanout dispatcher reads the ACTIVE-tenant set fresh on every tick, so schedule
    # membership needs no signal (see deploy/celery_fanout_design.md). Tenant delete: the
    # Domain post_delete cascade forgets the HOST snapshots; invalidate_tenant_deleted (below)
    # drops the SCHEMA snapshot, which has no host to derive from once the domains are gone.
    resolve_cache.forget_tenant(instance)


@receiver(post_delete, sender=Tenant)
def invalidate_tenant_deleted(sender: type[Tenant], instance: Tenant, **kwargs: Any) -> None:
    # Deterministic schema-snap cleanup on delete. The host snapshots are dropped by the Domain
    # post_delete cascade (dependents first), but by the time this fires the tenant's domains
    # are gone — so the schema-snap can't be derived from any host. Drop it explicitly by
    # schema_name (which survives on the instance). Closes the cold-cache delete gap without
    # relying on the reconcile orphan-sweep. See deploy/celery_fanout_design.md (schema-snap).
    resolve_cache.forget_schemas([instance.schema_name])