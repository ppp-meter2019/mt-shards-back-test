"""ContextVar carrying the active DB alias + shard-aware context managers.

current_db      - read by TenantDatabaseRouter on every ORM call. Set per
                  request by TenantShardRoutingMiddleware, or by the helpers
                  below for shell / management-command / admin-action code.
use_alias       - raw alias switch (no schema) for code that manages the
                  schema itself.
schema_context  - DROP-IN replacements for the django-tenants helpers of the
tenant_context    same names. The upstream versions switch ONLY the schema on
                  one connection (by default the 'default' one) and know
                  nothing about our router axis (current_db). For a sharded
                  tenant that sends the ORM to the default DB while the schema
                  is set elsewhere - the off-request variant of the same bug
                  the request path had. These versions wire BOTH axes and
                  restore both on exit.

All three are @contextmanager GENERATORS: the saved state (previous tenant +
current_db token) lives in the generator FRAME's locals, not on an instance, so
they are reentrancy / reuse / thread safe (nested `with`s and decorator reuse
each get their own frame) and the restore logic lives in ONE place (_switch).
Usable as `with ...:` and as `@decorator` (contextlib recreates the CM per call).

Import rule: project code imports these from `tenants.context`, never from
`django_tenants.utils`. TenantsConfig.ready() additionally monkeypatches the
upstream module so late importers get these versions too.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from django.db import connections
from django_tenants.utils import get_public_schema_name

if TYPE_CHECKING:                       # annotation-only: keeps this module free of the
    from .models import Tenant          # lazy-import cycle its runtime code avoids
    from .resolver import TenantSnapshot

# default=None is the SENTINEL for "no routing context established" (distinct from an
# explicit alias of "default"). Only middleware / use_alias / _switch set a real alias; when
# it is None the router can tell a genuine tenant query has no context and refuse (strict
# guard) instead of silently routing to the default DB (wrong shard). Readers that just want
# "an alias, default if unset" use active_alias().
current_db: ContextVar = ContextVar("current_db", default=None)


def active_alias() -> str:
    """EFFECTIVE alias, for readers that only need somewhere to look: the bound alias, or
    "default" when no routing context is established (compat, diagnostics). It COALESCES the
    unset state — do not use it where that distinction matters; use bound_alias()."""
    return current_db.get() or "default"


def bound_alias() -> str | None:
    """The alias explicitly BOUND to this context, or None when none is established.

    That None is load-bearing and must not be collapsed: it is what lets TenantDatabaseRouter
    tell a genuinely context-free query (a bug — the strict guard raises) from one deliberately
    routed to "default". The router is the only intended caller; every other reader wants
    active_alias(). Both exist so that nothing outside this module has to touch the ContextVar
    itself — enforced by scripts/ci_guard_routing_axis.sh.
    """
    return current_db.get()


@contextmanager
def use_alias(alias: str) -> Iterator[None]:
    """Set current_db for the duration of the with-block (no schema change).

    Use when the code manages the schema itself (e.g. raw cursors, DBA flows).
    """
    token = current_db.set(alias)
    try:
        yield
    finally:
        current_db.reset(token)


@contextmanager
def _switch(database: str, apply_to: Callable[[Any], None]) -> Iterator[None]:
    """Shared reentrancy-safe core of tenant_context / schema_context.

    axis 1: current_db -> `database` (read by TenantDatabaseRouter);
    axis 2: the schema on THAT shard's connection, applied by `apply_to(connection)`.
    Restores BOTH on exit (return OR exception). `token` and `prev_tenant` are FRAME
    locals — fresh per entry — so overlapping/nested/reused entries never clobber each
    other. Fixes an upstream quirk too: we save/restore the TARGET connection's own
    previous tenant (upstream saved the default connection's and restored it onto the
    target). `apply_to` runs INSIDE the try, so a failing schema-set still resets the token.
    """
    connection = connections[database]
    prev_tenant = connection.tenant          # previous of THIS connection
    token = current_db.set(database)         # axis 1: router
    try:
        apply_to(connection)                 # axis 2: schema on the shard connection
        yield
    finally:
        if prev_tenant is None:
            connection.set_schema_to_public()
        else:
            connection.set_tenant(prev_tenant)
        current_db.reset(token)


@contextmanager
def tenant_context(tenant: Tenant | TenantSnapshot, database: str | None = None) -> Iterator[None]:
    """Drop-in replacement for django_tenants' tenant_context (shard-aware).

    Wires BOTH axes: current_db -> the tenant's shard, and the tenant schema on that
    shard's connection. `database=` overrides the shard alias when needed.
    """
    with _switch(database or tenant.shard.alias, lambda conn: conn.set_tenant(tenant)):
        yield


@contextmanager
def schema_context(schema_name: str, database: str | None = None) -> Iterator[None]:
    """Drop-in replacement for django_tenants' schema_context (shard-aware).

    Resolves WHICH database to target from the tenant registry (schema_name ->
    Tenant.shard.alias) unless `database=` is given; `public` short-circuits to 'default'
    without a lookup. Prefer tenant_context(tenant) when you already hold the Tenant
    object - it skips the registry query.
    """
    with _switch(_resolve_database(schema_name, database),
                 lambda conn: conn.set_schema(schema_name)):
        yield


def _resolve_database(schema_name: str, database: str | None) -> str:
    """WHICH database a schema lives on: explicit `database` wins; `public` -> 'default';
    else the tenant registry (schema_name -> Tenant.shard.alias)."""
    if database:
        return database
    if schema_name == get_public_schema_name():
        return "default"
    from tenants.models import Tenant          # lazy: no app-registry cycles
    try:
        return (
            Tenant.objects.select_related("shard")
            .get(schema_name=schema_name)
            .shard.alias
        )
    except Tenant.DoesNotExist:
        raise Tenant.DoesNotExist(
            f"schema_context({schema_name!r}): no Tenant with this schema_name. "
            f"Pass database=<alias> explicitly for non-registered schemas (DBA/restore flows)."
        )
