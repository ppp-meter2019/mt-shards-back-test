"""Physical-state probes: what the SHARDS actually contain, as opposed to what the registry
claims.

The registry (`Tenant`, `Shard`, `Domain` on default.public) records intent; these answer
whether the schema is really there, how far it is migrated and who can log into it. That
gap is the whole point of the tenants console — a tenant row with `status=active` and no
schema is exactly the anomaly an operator opens the console to find.

Plain module-level functions, not viewset methods: they are a platform capability with
non-trivial batching, and nothing about them is HTTP. A host project that builds its own
console UI can call these directly. They take an ITERABLE OF TENANTS (already materialized
— see `_by_shard`), never a queryset to re-evaluate.

TWO PROPERTIES EVERY PROBE HERE SHARES, and the reason they live together:

  * BATCHED PER SHARD, never per tenant. Serializing one of these fields per row would mean
    an ORM round-trip inside tenant_context() for every tenant — a `SET search_path` PLUS a
    query each, i.e. 2N round-trips for a list, N separate degrade-and-log cycles when a
    shard is down, and N resets of django-tenants' search_path cache.
  * DEGRADE PER SHARD. A down or unreachable shard is logged and skipped; its tenants render
    with the field missing. The console is a DIAGNOSTIC tool — it must stay readable exactly
    when one cluster is broken. Only DB/connection failures are swallowed (`DBError`,
    `ConnectionDoesNotExist`); a programming error propagates and 500s, on purpose.

SQL SAFETY: schema names reach these queries as SQL IDENTIFIERS, so every one of them goes
through `quote_schema()` (validate AND quote in one call) and is pre-filtered on the safety
floor `is_safe_schema_identifier()`. That floor — not the stricter creation convention —
is the right gate here: a legacy name that predates `validate_schema_name` must still be
SERVED, it just must not be able to break out of quotes. The bare `'{s}'` string literals
are safe for the same reason (the floor excludes `'`).
"""
import logging

from django.db import Error as DBError, connections
from django.db.utils import ConnectionDoesNotExist

from tenants.validators import is_safe_schema_identifier, quote_schema
from users.models import User

logger = logging.getLogger(__name__)


def _by_shard(tenants) -> dict:
    """{shard_alias: {schema_name, ...}} — the grouping every probe starts from.

    `tenants` must be a materialized sequence, not a queryset: the caller runs all three
    probes over the same set, and a queryset handed in fresh to each would be evaluated
    three times.
    """
    out: dict[str, set[str]] = {}
    for t in tenants:
        out.setdefault(t.shard.alias, set()).add(t.schema_name)
    return out


def _for_each_shard(tenants, probe, what):
    """Run `probe(cursor, alias, schemas)` once per shard, degrading per shard.

    The grouping, the cursor, and the degrade-and-log cycle were copied in all three probes
    below; only the SQL differed. Keeping the skeleton here is what stops a future fix (a
    new exception type to catch, a retry, a metric) from landing in one copy and missing the
    other two. `schemas` arrives sorted so the generated SQL is stable and diffable.
    """
    for alias, schemas in _by_shard(tenants).items():
        try:
            with connections[alias].cursor() as cur:
                probe(cur, alias, sorted(schemas))
        except (DBError, ConnectionDoesNotExist):
            logger.warning("%s probe failed for shard %r; its tenants are shown without it",
                           what, alias, exc_info=True)


def existing_schemas(tenants) -> set:
    """{(shard_alias, schema_name)} for schemas that PHYSICALLY exist. One query per shard.

    A tenant absent from the result is rendered as "not confirmed" — which covers both
    "really missing" and "its shard was unreachable just now". The console must not claim
    the stronger of the two.
    """
    result: set = set()

    def probe(cur, alias, schemas):
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name = ANY(%s)",
            [schemas],
        )
        for (s,) in cur.fetchall():
            result.add((alias, s))

    _for_each_shard(tenants, probe, "schema_exists")
    return result


def last_migrations(tenants) -> dict:
    """{(shard_alias, schema_name): {"app", "name", "applied"}} — the most recently applied
    migration per tenant schema. At most TWO queries per shard regardless of tenant count:
    one to find which target schemas actually carry a `django_migrations` table, then one
    UNION over those picking the latest row per schema via DISTINCT ON.
    """
    result: dict = {}

    def probe(cur, alias, schemas):
        cur.execute(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_name = 'django_migrations' AND table_schema = ANY(%s)",
            [schemas],
        )
        # These names come from the DB, not from a request — but they are interpolated as
        # SQL identifiers below, so they still pass the safety floor (see module docstring).
        migrated = [s for (s,) in cur.fetchall() if is_safe_schema_identifier(s)]
        if not migrated:
            return
        union = " UNION ALL ".join(
            f"SELECT '{s}' AS schema, id, app, name, applied "
            f"FROM {quote_schema(s)}.django_migrations"
            for s in migrated
        )
        cur.execute(
            "SELECT DISTINCT ON (schema) schema, app, name, applied FROM ("
            + union
            + ") m ORDER BY schema, applied DESC NULLS LAST, id DESC"
        )
        for schema, app, name, applied in cur.fetchall():
            result[(alias, schema)] = {
                "app": app,
                "name": name,
                "applied": applied.isoformat() if applied else None,
            }

    _for_each_shard(tenants, probe, "last_migration")
    return result


def admins(tenants) -> dict:
    """{(shard_alias, schema_name): [{"id", "username", "is_active"}, ...]} — the
    `company_admin` users inside each tenant schema. Two queries per shard, same shape as
    last_migrations(): find which schemas carry the table, then one UNION ALL over those.

    Table and column names come from the model's `_meta`, so a `db_table` / `db_column`
    override cannot silently break this.

    Tenants whose schema_name fails the safety floor are dropped UP FRONT rather than inside
    the probe: that way a shard with nothing safe to ask is never opened at all (and never
    logs a spurious degrade warning), which is what the pre-refactor code did.
    """
    tenants = [t for t in tenants if is_safe_schema_identifier(t.schema_name)]
    table = User._meta.db_table
    col = {f: User._meta.get_field(f).column
           for f in ("id", "username", "is_active", "role")}
    result: dict = {}

    def probe(cur, alias, schemas):
        cur.execute(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_name = %s AND table_schema = ANY(%s)",
            [table, schemas],
        )
        present = [s for (s,) in cur.fetchall()]
        if not present:
            return
        # One UNION branch per schema. Bounded by the page size, so the host project's
        # pagination class is what keeps this plan small — this repo deliberately sets
        # none (see settings_base REST_FRAMEWORK).
        union = " UNION ALL ".join(
            f"SELECT '{s}' AS schema, {col['id']}, {col['username']}, "
            f"{col['is_active']} FROM {quote_schema(s)}.{table} "
            f"WHERE {col['role']} = %s"
            for s in present
        )
        cur.execute(union + " ORDER BY schema, 3", [User.Role.COMPANY_ADMIN] * len(present))
        for schema, uid, username, is_active in cur.fetchall():
            result.setdefault((alias, schema), []).append(
                {"id": uid, "username": username, "is_active": is_active})

    _for_each_shard(tenants, probe, "admins")
    return result
