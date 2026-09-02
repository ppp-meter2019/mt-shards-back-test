"""commons.platform — the mode-aware seam that lets ONE codebase run standalone or
multi-tenant (USE_MULTITENANT), for THIS project. NOT a generic reusable library.

Application/business code imports the primitives here (tenancy / admin / commands / beat /
mode) instead of importing `tenants` or `django_tenants` directly, so the same code runs in
both modes:
  * multitenant -> the real shard-aware helpers from the `tenants` app (imported lazily,
                   guarded by USE_MULTITENANT — `tenants` is absent in standalone);
  * standalone  -> no-ops / plain fallbacks (single default DB, no schemas, no fanout).

Dependency direction is ONE-WAY: `tenants` may import `commons.platform`, never the reverse
at module top level (any `tenants` import here is lazy + USE_MULTITENANT-guarded). One
BOUNDED, deliberate exception: `beat.FANOUT_TASK_NAME` is a bare string naming a `tenants`
task — commons is the PRODUCER of that beat entry, so it declares the contract and `tenants`
imports it UP; enforced live by the tenants.E005 system check. Because of this, do not treat
commons.platform as decoupled from the `tenants` task naming.
"""
