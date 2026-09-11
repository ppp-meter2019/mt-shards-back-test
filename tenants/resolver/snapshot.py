"""The routing snapshot — what `request.tenant` and the Celery worker actually hold.

This subsystem already called it that everywhere: the Redis keys are `host-snap:` and
`schema-snap:`, the reads are `get_snapshot` / `get_schema_snapshot`, and the carried
fields are `_SNAPSHOT_FIELDS`. This module gives the concept a TYPE instead of borrowing
the Tenant model for it, which fixes two things at once:

  * A snapshot rebuilt from the cache was a Tenant instance with every UNCARRIED field at
    its model default, so `request.tenant.company_name` returned the real value while the
    cache was cold and "" once it was warm — behaviour that depended on cache state, with
    only a comment forbidding reliance on it. Here those fields do not exist, so a stray
    read is an AttributeError instead of a plausible-looking wrong value.
  * A model instance carries save()/delete(), so a snapshot needed a `read_only` flag plus
    four method overrides on Tenant/Shard to refuse them. There is nothing to save here.

Two constructors, one per resolve path, so a HIT and a MISS cannot expose different things
— that equality is the entire point:

    TenantSnapshot.capture(tenant)     from a Tenant row   (DB resolve, worker _db_get)
    TenantResolveCache.load(payload)   from a cached dict   (cache hit)

NOT frozen, deliberately: django_tenants' TenantMainMiddleware assigns
`tenant.domain_url = hostname` immediately before `request.tenant = tenant`, so
`domain_url` is a declared mutable field rather than an attribute that materializes at
runtime. It is per-REQUEST and must never be cached — see _SNAPSHOT_FIELDS.
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:                       # annotation-only: the model must not be imported here
    from tenants.models import Tenant   # (this module is what replaces it on the hot path)


@dataclass
class ShardSnapshot:
    """The shard half of the snapshot.

    Nested rather than flattened to `shard_alias` so that tenants.context.tenant_context()
    — which reads `tenant.shard.alias` — accepts a real Tenant and a snapshot
    interchangeably, with no branch. The worker path relies on that: it hands over whichever
    of the two the cache produced.
    """

    id: int
    alias: str


@dataclass
class TenantSnapshot:
    """The tenant half: exactly the fields the request path and the worker read.

    Kept in step with TenantResolveCache._SNAPSHOT_FIELDS. Adding a field here means
    putting that value into Redis, so it is a deliberate act — and `domain_url` is the
    standing exception that must NOT go there.

    Consumers, for reference when changing this:
      schema_name  django_tenants' set_tenant() + setup_url_routing(); the status gate
      status       ShardAwareTenantMiddleware's ACTIVE/DEACTIVATED/NEW gate
      shard.alias  TenantShardRoutingMiddleware -> tenant_context() -> router axis
      id           business code (e.g. the S3 coordinate prefix keys on the numeric id)
    """

    id: int
    schema_name: str
    status: str
    shard: ShardSnapshot
    # Written by django_tenants' TenantMainMiddleware; declared so the write is visible
    # rather than surprising. Per-request — never serialized.
    domain_url: Optional[str] = None

    @classmethod
    def capture(cls, tenant: Union["Tenant", "TenantSnapshot"]) -> "TenantSnapshot":
        """Take the routing snapshot of a Tenant.

        Idempotent on a snapshot: TenantTask.get_tenant_for_schema returns either a cached
        snapshot or a fresh DB row, and both reach tenant_context() and dump().
        """
        if isinstance(tenant, cls):
            return tenant
        return cls(
            id=tenant.id,
            schema_name=tenant.schema_name,
            status=tenant.status,
            shard=ShardSnapshot(id=tenant.shard.id, alias=tenant.shard.alias),
        )
