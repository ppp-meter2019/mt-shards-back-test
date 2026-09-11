"""Operator console — the management UI/API over the tenant registry.

A SUBPACKAGE, deliberately not a separate Django app: an app boundary in Django is
fundamentally about models + migrations, and this package owns NO models. It would buy an
INSTALLED_APPS entry and a second AppConfig.ready() (neither of which anything here needs)
while forcing a SHARED_APPS/TENANT_APPS answer for an app that has no tables. Promote it to
a real app the day the console needs a model of its own (an operator audit log, saved
filters, a provisioning queue) — that is when migrations, and therefore an app, start
paying for themselves.

BOUNDARY (one-way, enforced by scripts/ci_guard_console_boundary.sh):

    tenants.console  ->  tenants        allowed (models, validators, permissions, context)
    tenants          ->  tenants.console        FORBIDDEN

Nothing on the request-serving path — middleware, resolver, router, celery, checks — may
import from here: importing the whole runtime stack pulls in neither this package nor DRF's
viewsets/serializers (verified, and kept that way by the guard).

NB that is a statement about the REQUEST PATH, not about process startup. A normal boot DOES
load this package, because django.contrib.admin's autodiscovery imports tenants/admin.py,
which is the shim that pulls in console/admin.py. Dropping the console from a deployment
therefore means dropping three things together — this package, that shim, and the console
imports in urls_public.py — not this package alone.

WHAT LIVES HERE
  views.py        DRF viewsets for Shard / Tenant / ReservedHostRule + the base-domains list
  serializers.py  their serializers
  admin.py        `public_admin_site` and its ModelAdmins (mounted only on the public host)
  probes.py       physical-state probes (does the schema exist, last migration, admins) —
                  batched per shard, degrading per shard

NB `health` stays in tenants/views.py: it is imported by BOTH urls_public and urls_tenant,
so it is runtime, not console. And tenants/admin.py remains as a one-line shim because
Django's admin autodiscovery imports `<app>/admin.py` by convention — see the note there.
"""
