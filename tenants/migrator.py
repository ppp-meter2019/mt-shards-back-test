"""Zero-downtime tenant migrator — core module (MT only; the `tenants` app is not
installed in standalone, so neither is this).

Design: deploy/migrations_plan_rev7.md. Contents (steps 1–7 of §21):

  migrate_tenant(), migrate_shared()     the per-target algorithm                §6
  snapshot()                             a target's state after a hard stop      §9.7
  lease_holders(), lease_force_release(),  read side for migration_lease and      §10, §11
    behind(), public_plan()                reconcile_tenants
  check_reactivation(), schema_table_count()  reactivation stopper (admin, console)  §5.5, §14
  start_run_dir(), per-target log files  one folder per CLI run, tee to a file   §9.9
  Result, RunOptions, Reason, status groups                                      §2.1, §5.3, §9.10

  owner(), LEASE_TTL                     process identity for the lease          §4.2
  lease_acquire/release/holder,          the MigrationLease mutex (M1)           §4.3–§4.5
    shard_session_active, _fenced_cas
  _fresh(), _open_shard(), _close_shard()  connection hygiene                    §5.1, §5.2, §7
  pending_plan(), MISSING                unapplied migrations of a tenant (B1)   §5.4
  schema_has_relations(), code_older_than_db()                                   §5.5
  is_conn_error(), is_lock_timeout()     exception classification                §5.5
  invalid_indexes()                      INVALID indexes left by a failed CIC    §5.5

Routing rule (§5.3): the lease and every status CAS go to `default`; everything that
touches a tenant schema goes to `connections[t.shard.alias]`.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import socket
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, NamedTuple

from django.conf import settings
from django.db import InterfaceError, OperationalError, connections
from django.db.models import Exists
from django.db.models.functions import Now
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django_tenants.migration_executors.base import run_migrations
from django_tenants.utils import get_tenant_base_migrate_command_class, schema_exists

from tenants.models import MigrationLease, Tenant
from tenants.resolver import resolve_cache
from tenants.validators import SCHEMA_NAME_RE, quote_schema

logger = logging.getLogger("tenants.migrate")

# Fixed TTL, never extended (decision 1.2.11). A safety net, not a guarantee: no lease —
# free, expired or released after a lost connection — is taken while a tm:<key> backend
# is still busy on the shard (M1, lease_acquire).
LEASE_TTL = timedelta(hours=1)

_LEASE_TABLE = MigrationLease._meta.db_table


# ---------------------------------------------------------------------------
# owner() — host:pid:process_start_utc (M2)
# ---------------------------------------------------------------------------

_owner_cache: tuple[int, str] | None = None


def _process_start_utc() -> str:
    """Real start time of THIS process: field 22 of /proc/self/stat (clock ticks since
    boot) + `btime` from /proc/stat. Linux-only, like PR_SET_PDEATHSIG; psutil is not a
    dependency."""
    with open("/proc/self/stat") as f:
        stat = f.read()
    # Field 2 (comm) is parenthesised and may itself contain spaces or ')': split after
    # the LAST ')'. What follows starts at field 3, so field 22 is index 19.
    start_ticks = int(stat[stat.rindex(")") + 2:].split()[19])
    with open("/proc/stat") as f:
        btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
    secs = btime + start_ticks / os.sysconf("SC_CLK_TCK")
    return datetime.fromtimestamp(int(secs), dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def owner() -> str:
    """This process's lease identity, e.g. ``app-1:4312:2026-10-10T12:00:03Z``.

    Cached per PID, never as a module constant: fork-pool workers and Celery prefork
    children would inherit the parent's value and the fence could not tell them apart.
    Only the process itself compares it (``owner = :me``); nobody parses it — a human
    checks host/pid/start against ``ps -o lstart= -p <pid>`` to rule out a reused pid.
    """
    global _owner_cache
    pid = os.getpid()
    if _owner_cache is None or _owner_cache[0] != pid:       # new process after fork
        _owner_cache = (pid, f"{socket.gethostname()}:{pid}:{_process_start_utc()}")
    return _owner_cache[1]


# ---------------------------------------------------------------------------
# Connections (§5.1, §5.2, §7)
# ---------------------------------------------------------------------------

def _fresh() -> None:
    """Close `default` before each migrator access to it (lease, CAS, tenant read), so we
    depend on neither CONN_MAX_AGE nor the pooler. A no-op inside atomic (tests)."""
    c = connections["default"]
    if not c.in_atomic_block:
        c.close()


def _open_shard(alias: str, key: int) -> None:
    """Arm `connections[alias]` — the same object run_migrations will use — with the
    migrator's lock_timeout and the diagnostic application_name ``tm:<key>`` (what M1 and
    ``migration_lease --release`` look for)."""
    c = connections[alias]
    c.ensure_connection()
    with c.cursor() as cur:
        cur.execute(
            "SELECT set_config('lock_timeout', %s, false), set_config('application_name', %s, false)",
            [settings.TENANT_MIGRATE_DDL_LOCK_TIMEOUT, f"tm:{key}"],
        )


def _close_shard(alias: str) -> None:
    """Always called in `finally`: close the shard connection so lock_timeout dies with the
    server session (I8). Inside someone else's atomic — only in tests — reset instead.

    Also points the connection wrapper back at `public`. pending_plan() leaves it on the
    tenant schema with include_public=False, and upstream run_migrations resets it only
    on success (django_tenants/migration_executors/base.py:74); the next user of this
    alias in a long-lived process (a Celery worker) must not inherit either.
    """
    c = connections[alias]
    if c.in_atomic_block:                       # tests (TestCase) only
        with c.cursor() as cur:
            cur.execute("SET lock_timeout TO DEFAULT")
            cur.execute("SET application_name TO DEFAULT")
    else:
        _reset_session(c.connection)
        c.close()
    c.set_schema_to_public()


def _reset_session(raw: Any) -> None:
    """Best effort, before close(): RESET ALL on an idle session. Closing the client side
    should end the server session and its lock_timeout / application_name tm:<key> with
    it — but behind RDS Proxy a dropped client's database connection "stays open and is
    returned to the connection pool", and what happens to a PINNED one (set_config pins)
    is undocumented. Reset first, so app traffic never inherits the migrator's 5s
    lock_timeout and a pooled backend never poses as a running tm:<key> migration.
    A session still running a statement (after a hard Ctrl-C) is left alone: a new
    command would fail or wait; such a backend ends with its client anyway."""
    if raw is None:
        return
    try:
        from psycopg.pq import TransactionStatus
        status = raw.info.transaction_status
        if status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
            raw.rollback()
            status = raw.info.transaction_status
        if status == TransactionStatus.IDLE:
            raw.execute("RESET ALL")
            if not raw.autocommit:               # else close() would roll the RESET back
                raw.commit()
    except Exception:                            # broken connection: nothing to reset
        pass


# ---------------------------------------------------------------------------
# MigrationLease (§4.3–§4.5)
# ---------------------------------------------------------------------------

class ShardUnavailable(Exception):
    """The tenant's shard (not `default`) could not be reached."""


class LeaseHolder(NamedTuple):
    owner: str
    since: datetime
    until: datetime
    expired: bool


class ShardSession(NamedTuple):
    pid: int
    running: timedelta
    query: str


# A backend labelled tm:<key> that may still be doing migration work. `state` and
# `query_start` are NULL for a session of ANOTHER role unless this role has
# pg_read_all_stats (application_name stays visible): such a session counts as busy —
# being unable to see that it is idle must never let a second migrator start.
_SESSION_ACTIVE_SQL = (
    "SELECT pid, coalesce(clock_timestamp() - query_start, interval '0'), left(query, 80)"
    "  FROM pg_stat_activity"
    " WHERE datname = current_database()"
    "   AND application_name = %s"
    "   AND coalesce(state, 'unknown') <> 'idle'"
    "   AND pid <> pg_backend_pid()"
    " ORDER BY query_start NULLS FIRST LIMIT 1"
)


def shard_session_active(alias: str, key: int) -> ShardSession | None:
    """A non-idle backend labelled ``tm:<key>`` on `alias` (``default`` for key 0), i.e.
    migration work for this key is still running server-side — possibly after its client
    was SIGKILLed or lost its connection. Shared by lease_acquire (M1), ``migration_lease
    --release`` and reconcile_tenants.

    Blind spot, by design: a LIVE owner between two statements outside a transaction
    (between migrations, between the steps of an atomic=False migration) is `idle` and
    is not seen. That matters only for an EXPIRED lease — one tenant migrating for longer
    than LEASE_TTL, which is not supported (deploy/migrations_plan_rev7.md §17) — and for
    an operator's ``--release``, which therefore refuses a non-expired lease without
    ``--force``.

    pg_stat_clear_snapshot(): pg_stat_activity is snapshotted once per transaction, and
    inside a long transaction (TestCase) a second look would replay the first.
    clock_timestamp(), not now(), for the same reason.
    """
    with connections[alias].cursor() as cur:
        cur.execute("SELECT pg_stat_clear_snapshot()")
        cur.execute(_SESSION_ACTIVE_SQL, [f"tm:{key}"])
        row = cur.fetchone()
    return ShardSession(*row) if row else None


def lease_holder(key: int) -> LeaseHolder | None:
    """Current holder of lease `key`, or None if free / never taken."""
    _fresh()
    with connections["default"].cursor() as cur:
        cur.execute(
            f"SELECT owner, since, until, until < now() FROM {_LEASE_TABLE}"
            f" WHERE key = %s AND owner IS NOT NULL",
            [key],
        )
        row = cur.fetchone()
    return LeaseHolder(*row) if row else None


def lease_holders() -> dict[int, LeaseHolder]:
    """Every held lease (owner IS NOT NULL) by key, in one query — for the reports that
    walk all tenants (migration_lease --list, reconcile_tenants)."""
    _fresh()
    with connections["default"].cursor() as cur:
        cur.execute(
            f"SELECT key, owner, since, until, until < now() FROM {_LEASE_TABLE}"
            f" WHERE owner IS NOT NULL ORDER BY key"
        )
        return {row[0]: LeaseHolder(*row[1:]) for row in cur.fetchall()}


def lease_lock_for_transition(key: int) -> LeaseHolder | None:
    """Lock lease row `key` FOR UPDATE inside the CALLER's transaction on `default`
    (creating it free if absent) and return its live holder, if any.

    Used by the admin before writing MAINTENANCE (§14.2): until that transaction commits,
    a migrator's lease_acquire on this key WAITS on the row lock, then re-reads the status
    and finds MAINTENANCE; a migrator that got the lease first is returned here and the
    admin refuses. That closes the window between the form's check and the status write.
    Lock order is lease row → tenant row, the same as the migrator's, so no deadlock."""
    c = connections["default"]
    if not c.in_atomic_block:
        raise RuntimeError("lease_lock_for_transition() must run inside transaction.atomic()")
    with c.cursor() as cur:
        cur.execute(
            f"INSERT INTO {_LEASE_TABLE} (key, owner, since, until) VALUES (%s, NULL, NULL, NULL)"
            f" ON CONFLICT (key) DO NOTHING",
            [key],
        )
        cur.execute(
            f"SELECT owner, since, until, until < now() FROM {_LEASE_TABLE}"
            f" WHERE key = %s FOR UPDATE",
            [key],
        )
        row = cur.fetchone()
    if row is None or row[0] is None or row[3]:
        return None
    return LeaseHolder(*row)


def lease_force_release(key: int, expected_owner: str) -> bool:
    """Operator release (``migration_lease --release``): free lease `key` iff it is still
    held by `expected_owner` — the owner the operator was shown and checked. A run that
    re-took the lease in between is not touched. True if a row was released."""
    _fresh()
    with connections["default"].cursor() as cur:
        cur.execute(
            f"UPDATE {_LEASE_TABLE} SET owner = NULL, since = NULL, until = NULL"
            f" WHERE key = %s AND owner = %s",
            [key, expected_owner],
        )
        released = cur.rowcount == 1
    if released:
        logger.warning("lease %s released by operator (was %s)", key, expected_owner,
                       extra={"lease_key": key, "lease_owner": expected_owner})
    return released


def _locked_msg(h: LeaseHolder | None) -> str:
    if h is None:                               # freed between our CAS and this read
        return "locked (lease changed hands concurrently)"
    return f"locked by {h.owner} since {h.since:%Y-%m-%dT%H:%M:%SZ}"


def lease_acquire(key: int, alias: str) -> str | None:
    """Take lease `key` for owner(). Returns None when acquired, otherwise the human
    ``locked …`` reason (the caller turns it into skipped). Never waits.

    M1, for EVERY acquisition — not only for an expired lease: first ask `alias` (the
    tenant's shard; `default` for key 0) whether a backend labelled ``tm:<key>`` is still
    working. Such a backend outlives its client: after a SIGKILL, and after a lost
    connection (proxy failover, network blip) — where the migrator reports conn_error and
    RELEASES the lease while the server keeps running the migration. Taking the lease then
    would re-plan and re-run the unrecorded migration in parallel. A long legitimate
    migration that outlives the TTL is the same case.

    Then one upsert takes a free or expired lease. Under READ COMMITTED, ON CONFLICT DO
    UPDATE re-checks its WHERE against the latest committed row, so of two claimants
    exactly one wins, and a lease someone re-took a moment ago (fresh `until`) is not taken.
    """
    _fresh()                                    # covers alias == "default" (key 0)
    try:
        busy = shard_session_active(alias, key)
    except Exception as e:
        if is_conn_error(e):                    # let callers tell shard from default
            raise ShardUnavailable(alias) from e
        raise
    if busy is not None:
        h = lease_holder(key)
        if h is not None and not h.expired:
            return _locked_msg(h)
        state = "lease expired" if h is not None else "lease free"
        return (f"locked ({state}, backend still active: pid {busy.pid}, "
                f"running {int(busy.running.total_seconds() // 60)} min)")
    _fresh()
    with connections["default"].cursor() as cur:
        cur.execute(
            f"INSERT INTO {_LEASE_TABLE} (key, owner, since, until)"
            f" VALUES (%s, %s, now(), now() + %s)"
            f" ON CONFLICT (key) DO UPDATE"
            f"   SET owner = EXCLUDED.owner, since = EXCLUDED.since, until = EXCLUDED.until"
            f" WHERE {_LEASE_TABLE}.owner IS NULL OR {_LEASE_TABLE}.until < now()"
            f" RETURNING 1",
            [key, owner(), LEASE_TTL],
        )
        won = cur.fetchone() is not None
    return None if won else _locked_msg(lease_holder(key))


def lease_release(key: int) -> bool:
    """Free lease `key` iff owner() holds it. True if a row was released."""
    _fresh()
    with connections["default"].cursor() as cur:
        cur.execute(
            f"UPDATE {_LEASE_TABLE} SET owner = NULL, since = NULL, until = NULL"
            f" WHERE key = %s AND owner = %s",
            [key, owner()],
        )
        return cur.rowcount == 1


def _fenced_cas(pk: int, expected: tuple[str, ...], **fields: Any) -> int:
    """Status CAS fenced by the lease (§4.4): writes only while the tenant is still in
    `expected` AND owner() holds its lease. Returns rows updated (0 or 1); the caller
    forgets the tenant on 1 and diagnoses on 0.

    The fence checks ``owner = me`` WITHOUT ``until > now()``: a takeover always rewrites
    `owner` (lease_acquire), so that is enough — and a legitimate provisioning that
    outlives the TTL can still record ACTIVE.
    """
    _fresh()
    return (Tenant.objects
            .filter(pk=pk, status__in=expected)
            .filter(Exists(MigrationLease.objects.filter(key=pk, owner=owner())))
            .update(status_changed_at=Now(), **fields))


# ---------------------------------------------------------------------------
# Schema state (§5.4, §5.5)
# ---------------------------------------------------------------------------

MISSING = object()        # pending_plan(): the tenant schema does not exist on its shard


def pending_plan(t: Any) -> list[tuple[str, str]] | object:
    """Unapplied migrations of tenant `t` as [(app_label, name)], or MISSING.

    B1: the schema-existence check comes first, so "no schema" is never confused with
    "schema behind". PostgreSQL silently drops a nonexistent schema from search_path; with
    this stack (django-tenants introspection pins search_path to the tenant schema in
    has_table) a missing schema then reads as "nothing applied" — a FULL plan, verified by
    PendingPlanTests with the guard removed — and upstream's warning about picking up
    public's django_migrations (base.py:47-49) is the neighbouring trap. Either way, only
    the explicit check yields MISSING. include_public=False: an existing but empty schema
    reads as "nothing applied" → full plan.
    """
    alias = t.shard.alias
    if not schema_exists(t.schema_name, alias):
        return MISSING
    c = connections[alias]
    try:
        c.set_schema(t.schema_name, include_public=False)   # django_migrations of the tenant only
        ex = MigrationExecutor(c)
        return [(m.app_label, m.name)
                for m, backwards in ex.migration_plan(ex.loader.graph.leaf_nodes())
                if not backwards]
    finally:
        _close_shard(alias)


def behind(t: Any) -> int | None:
    """Number of unapplied migrations of tenant `t`; None when its schema is missing.
    The one definition of "behind" for snapshot() and reconcile_tenants."""
    plan = pending_plan(t)
    return None if plan is MISSING else len(plan)


def public_plan() -> list[tuple[str, str]]:
    """Unapplied migrations of `public` on `default` — what `migrate_schemas --shared`
    would apply. Migrate records every app's migrations in each schema it runs in (the
    router only skips the operations), so the whole graph is the target here too."""
    _fresh()
    c = connections["default"]
    try:
        c.set_schema_to_public()
        ex = MigrationExecutor(c)
        return [(m.app_label, m.name)
                for m, backwards in ex.migration_plan(ex.loader.graph.leaf_nodes())
                if not backwards]
    finally:
        _close_shard("default")


def schema_table_count(alias: str, schema: str) -> int | None:
    """Tables in `schema` on `alias`, or None when the schema does not exist — what the admin
    shows next to the FAILED → NEW confirmation (§14.2). Connection errors propagate."""
    with connections[alias].cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid"
            "        AND c.relkind IN ('r', 'p'))"
            "  FROM pg_namespace n WHERE n.nspname = %s",
            [schema],
        )
        row = cur.fetchone()
    return None if row is None else int(row[0])


class ReactivationBlocked(Exception):
    """check_reactivation(): the tenant must not become ACTIVE yet. `code` is the console's
    machine code, `status` its HTTP status (409: fix the tenant; 503: retry later)."""
    code = "reactivation_blocked"
    status = 409


class DeployInProgress(ReactivationBlocked):
    code, status = "deploy_in_progress", 503


class SchemaMissing(ReactivationBlocked):
    code = "schema_missing"


class SchemaBehind(ReactivationBlocked):
    code = "schema_behind"

    def __init__(self, plan: list[tuple[str, str]]) -> None:
        super().__init__(f"{len(plan)} migration(s) not applied")
        self.pending = [f"{a}.{n}" for a, n in plan]


class ReactivationUnavailable(ReactivationBlocked):
    """`default` or the tenant's shard could not be reached (wraps ShardUnavailable)."""
    code, status = "shard_unreachable", 503


def check_reactivation(t: Any) -> None:
    """The reactivation stopper (§5.5, §14): raise unless tenant `t` may be served as ACTIVE
    by THIS code right now — its schema exists and has every migration this code knows,
    and no newer release has already migrated public (then a deploy is under way and this
    process is the old code). Read-only."""
    try:
        older = code_older_than_db()
    except Exception as e:
        if is_conn_error(e):
            raise ReactivationUnavailable("default unreachable") from ShardUnavailable("default")
        raise
    if older:
        raise DeployInProgress("a newer release has already migrated public; retry after the deploy")
    alias = t.shard.alias
    try:
        plan = pending_plan(t)
    except Exception as e:
        if is_conn_error(e):
            raise ReactivationUnavailable(f"shard {alias} unreachable") from ShardUnavailable(alias)
        raise
    if plan is MISSING:
        raise SchemaMissing(f'schema "{t.schema_name}" does not exist on {alias}')
    if plan:
        raise SchemaBehind(plan)


def schema_has_relations(alias: str, schema: str) -> bool:
    """Any relation (table, index, sequence, view…) in `schema` on `alias`."""
    with connections[alias].cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s)",
            [schema],
        )
        return cur.fetchone()[0]


def code_older_than_db() -> bool:
    """True if public.django_migrations holds migrations this code does not know AND they
    were applied after the newest known one — i.e. a newer release already migrated the DB.

    The "applied later" heuristic lets a migration deleted from the code (but still in the
    table, with an earlier `applied`) not block provisioning forever. Only apps in
    loader.migrated_apps count. `applied` is the client's timezone.now(): clock skew must
    stay far below the interval between releases. An unreachable `default` raises.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    known = set(loader.disk_migrations)
    for m in loader.disk_migrations.values():
        known.update(m.replaces or ())                       # squash: replaced names are known too
    _fresh()
    c = connections["default"]
    c.set_schema_to_public()
    applied = MigrationRecorder(c).applied_migrations()
    relevant = {k: r for k, r in applied.items() if k[0] in loader.migrated_apps}
    unknown = {k: r for k, r in relevant.items() if k not in known}
    if not unknown:
        return False
    latest_known = max((r.applied for k, r in relevant.items() if k in known), default=None)
    return latest_known is None or any(r.applied > latest_known for r in unknown.values())


# ---------------------------------------------------------------------------
# Exception classification (§5.5)
# ---------------------------------------------------------------------------

def _driver_error_classes() -> tuple[type[BaseException], ...]:
    classes: list[type[BaseException]] = [OperationalError, InterfaceError]  # Django wrappers
    try:
        import psycopg
        classes += [psycopg.OperationalError, psycopg.InterfaceError]
    except ImportError:                         # pragma: no cover — psycopg is the driver
        pass
    try:
        import psycopg2
        classes += [psycopg2.OperationalError, psycopg2.InterfaceError]
    except ImportError:                         # pragma: no cover
        pass
    return tuple(classes)


_CONN_ERROR_CLASSES = _driver_error_classes()
# The server ended (or never gave) the session: admin/crash shutdown, cannot connect now,
# and its own session timeouts — idle_session_timeout (57P05) and
# idle_in_transaction_session_timeout (25P03) close the connection just the same.
# Errors raised while CONNECTING (e.g. 53300 too many connections) carry no SQLSTATE in
# psycopg 3 and are caught by the `None` rule in is_conn_error (verified 2026-10-13).
_CONN_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "57P05", "25P03"})


def _chain(e: BaseException | None):
    """e, then its __cause__/__context__ links (cycle-safe)."""
    seen: set[int] = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        yield e
        e = e.__cause__ or e.__context__


def _sqlstate(e: BaseException) -> str | None:
    """First SQLSTATE found along the chain. Django's wrappers carry none themselves; the
    driver exception they wrap (as __cause__) does — psycopg `.sqlstate`, psycopg2 `.pgcode`."""
    for x in _chain(e):
        code = getattr(x, "sqlstate", None) or getattr(x, "pgcode", None)
        if code:
            return code
    return None


def is_conn_error(e: BaseException) -> bool:
    """Lost/unusable connection, anywhere in the __cause__/__context__ chain:
      * any driver error whose OWN SQLSTATE is class 08 or in _CONN_SQLSTATES, whatever its
        class — 25P03 (idle_in_transaction_session_timeout) is an InternalError in psycopg,
        not an OperationalError, yet the server has closed the session all the same;
      * an OperationalError/InterfaceError whose SQLSTATE is None (connect failures, a
        dropped socket) or one of the above.
    A lock timeout (55P03) or a statement cancel (57014) is an OperationalError too, and is
    NOT a connection error."""
    for x in _chain(e):
        own = getattr(x, "sqlstate", None) or getattr(x, "pgcode", None)
        if own and (own.startswith("08") or own in _CONN_SQLSTATES):
            return True
        if isinstance(x, _CONN_ERROR_CLASSES):
            code = _sqlstate(x)
            if code is None or code.startswith("08") or code in _CONN_SQLSTATES:
                return True
    return False


def is_lock_timeout(e: BaseException) -> bool:
    """SQLSTATE 55P03 (lock_not_available) anywhere in the chain."""
    return any((getattr(x, "sqlstate", None) or getattr(x, "pgcode", None)) == "55P03"
               for x in _chain(e))


def invalid_indexes(alias: str, schema: str) -> tuple[str, ...]:
    """INVALID indexes in `schema` (a failed CREATE INDEX CONCURRENTLY leaves one). Run on
    its own short query after _close_shard. A live CIC also reads as invalid, hence the
    lease/session columns next to it in `reconcile --report`. Errors only hide the list."""
    try:
        with connections[alias].cursor() as cur:
            cur.execute(
                "SELECT c.relname FROM pg_index i"
                "  JOIN pg_class c ON c.oid = i.indexrelid"
                "  JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE NOT i.indisvalid AND n.nspname = %s ORDER BY 1",
                [schema],
            )
            return tuple(r[0] for r in cur.fetchall())
    except Exception:
        logger.warning("invalid_indexes check failed for %s on %s", schema, alias, exc_info=True)
        return ()


# ---------------------------------------------------------------------------
# Status groups, Result, RunOptions (§2.1, §5.3, §9.10)
# ---------------------------------------------------------------------------

PROVISIONABLE = (Tenant.Status.NEW, Tenant.Status.PENDING)
UPGRADABLE = (Tenant.Status.ACTIVE, Tenant.Status.DEACTIVATED)
OPERATOR_OWNED = (Tenant.Status.FAILED, Tenant.Status.MAINTENANCE)   # never in a --tenant run


class Reason:
    """Machine codes for Result.reason / --summary-json (§9.10). `msg` is for humans."""
    APPLIED = "applied"
    RESUMED = "resumed"
    TARGETED = "targeted"
    UP_TO_DATE = "up_to_date"
    DONE_BY_ANOTHER_RUN = "done_by_another_run"
    PROVISIONING_ELSEWHERE = "provisioning_elsewhere"
    LOCKED = "locked"
    STATUS_FAILED = "status_failed"
    STATUS_MAINTENANCE = "status_maintenance"
    STATUS_CHANGED = "status_changed"
    LEASE_LOST = "lease_lost"
    TENANT_DELETED = "tenant_deleted"
    CODE_OLDER_THAN_DB = "code_older_than_db"
    SCHEMA_MISSING = "schema_missing"
    SCHEMA_EXISTS = "schema_exists"
    LOCK_TIMEOUT = "lock_timeout"
    MIGRATION_ERROR = "migration_error"
    INCOMPLETE_AFTER_RUN = "incomplete_after_run"
    CONN_ERROR = "conn_error"
    DEFAULT_SHARD_ANOMALY = "default_shard_anomaly"
    SHARD_UNREACHABLE = "shard_unreachable"
    STOPPED = "stopped"
    HARD_STOP = "hard_stop"

    @classmethod
    def all(cls) -> tuple[str, ...]:
        return tuple(v for k, v in vars(cls).items() if k.isupper())


class Result(NamedTuple):
    kind: str                       # "ok" | "noop" | "failed" | "skipped"
    schema: str
    shard: str
    reason: str = ""                # Reason.* (machine)
    msg: str = ""                   # human text
    secs: float = 0.0
    invalid_indexes: tuple[str, ...] = ()
    conn_error: str | None = None   # None | "shard" | "default"


@dataclass(frozen=True)
class RunOptions:
    verbosity: int = 1
    fake: bool = False
    fake_initial: bool = False
    prune: bool = False
    run_syncdb: bool = False
    app_label: str | None = None
    migration_name: str | None = None
    maintenance: bool = False       # allow migrating a MAINTENANCE tenant (-s X --maintenance)
    log_dir: Path | None = None     # None → no per-tenant log files (Celery)

    @property
    def targeted(self) -> bool:
        return bool(self.fake or self.fake_initial or self.prune or self.run_syncdb
                    or self.app_label or self.migration_name)


class Incomplete(Exception):
    """run_migrations returned, yet the tenant still has unapplied migrations (I4)."""

    def __init__(self, remaining: int) -> None:
        super().__init__(f"{remaining} migration(s) still unapplied after the run")
        self.remaining = remaining


_upstream_defaults_cache: dict[str, Any] | None = None


def _upstream_options(opts: RunOptions, alias: str) -> dict[str, Any]:
    """Options for upstream run_migrations: the upstream `migrate` parser defaults (built
    once per process) plus our overrides. `database` is the ONLY way the shard reaches
    upstream (§5.3) — without it everything would run on `default`."""
    global _upstream_defaults_cache
    if _upstream_defaults_cache is None:
        cmd = get_tenant_base_migrate_command_class()()
        _upstream_defaults_cache = vars(cmd.create_parser("manage.py", "migrate").parse_args([]))
    return {
        **_upstream_defaults_cache,
        "database": alias, "interactive": False, "skip_checks": True,
        "plan": False, "check_unapplied": False,
        "verbosity": opts.verbosity,
        "fake": opts.fake, "fake_initial": opts.fake_initial, "prune": opts.prune,
        "run_syncdb": opts.run_syncdb,
        "app_label": opts.app_label, "migration_name": opts.migration_name,
    }


# ---------------------------------------------------------------------------
# Per-run log folders and per-target log files (§9.9)
# ---------------------------------------------------------------------------

KEEP_PREVIOUS_RUNS = 2
# Run-level files start with "_": SCHEMA_NAME_RE makes every valid schema start with
# [a-z0-9], so no tenant's own file can ever be named like them.
SHARED_LOG = "_shared.log"
SUMMARY_LOG = "_summary.log"
RUN_DIR_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d-\d\d-\d\dZ_.+_\d+$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# upstream run_migrations' line prefix: "[standard:acme] " or "[1/9 (11%) standard:acme] "
_UPSTREAM_PREFIX_RE = re.compile(r"^\[(?:\d+/\d+ \(\d+%\) )?[^\s:\]]+:[^\s\]]+\] ")


def start_run_dir(root: Path, started: datetime) -> Path:
    """Create this run's folder ``<root>/<started UTC>_<host>_<pid>`` after deleting all but
    the KEEP_PREVIOUS_RUNS newest earlier run folders. Names start with the UTC timestamp,
    so name order is age order; anything not matching RUN_DIR_RE is left alone."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    previous = sorted(p.name for p in root.iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name))
    for name in previous[:-KEEP_PREVIOUS_RUNS]:
        shutil.rmtree(root / name, ignore_errors=True)
    run = root / f"{started:%Y-%m-%dT%H-%M-%SZ}_{socket.gethostname()}_{os.getpid()}"
    run.mkdir(exist_ok=True)
    return run


def tenant_log_name(schema: str, pk: int) -> str:
    """``<schema>.log`` for a schema name the validators would accept today; otherwise
    (a legacy or bypassed row — the model field itself allows nearly anything, "/" and ".."
    included) ``_tenant_<pk>.log``, which can neither collide with a run-level file nor
    escape the run folder. The real schema name is in the file's header and the summary."""
    return f"{schema}.log" if SCHEMA_NAME_RE.fullmatch(schema) else f"_tenant_{pk}.log"


def _clean(line: str) -> str:
    return _UPSTREAM_PREFIX_RE.sub("", _ANSI_RE.sub("", line))


class _Tee(io.TextIOBase):
    """Write-through to the live stream; complete lines also go to the target's file."""

    def __init__(self, live: Any, log: _TargetLog) -> None:
        self._live, self._log, self._buf = live, log, ""

    def write(self, s: str) -> int:
        try:
            self._live.write(s)
        except (OSError, ValueError):       # the terminal/pipe went away (Ctrl-C killed
            pass                            # `| tee`): the file still gets every line
        *lines, self._buf = (self._buf + s).split("\n")
        for line in lines:
            self._log.line(line)
        return len(s)

    def flush(self) -> None:
        try:
            self._live.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:               # keeps upstream's colours on a terminal
        return self._live.isatty()

    @property
    def encoding(self) -> str:              # type: ignore[override]
        return getattr(self._live, "encoding", None) or "utf-8"

    def drain(self) -> None:
        if self._buf:
            self._log.line(self._buf)
            self._buf = ""


class _TargetLog:
    """``<run dir>/<tenant_log_name()>`` (SHARED_LOG for --shared): a header, the FULL
    run_migrations output without upstream's prefix, the traceback of a failure, and a
    final result line. Flushed per line, so a SIGKILL keeps what was written. One target
    per process at a time, so each file has exactly one writer."""

    def __init__(self, path: Path, header: dict[str, str]) -> None:
        self._f = open(path, "a", encoding="utf-8")
        for k, v in header.items():
            self.line(f"{k}: {v}")
        self.line("-" * 72)
        self._saved = (sys.stdout, sys.stderr)
        self._tees = (_Tee(sys.stdout, self), _Tee(sys.stderr, self))
        sys.stdout, sys.stderr = self._tees

    def line(self, text: str) -> None:
        self._f.write(_clean(text) + "\n")
        self._f.flush()

    def note(self, text: str) -> None:
        for line in text.rstrip("\n").split("\n"):
            self.line(line)

    def close(self, footer: str) -> None:
        for tee in self._tees:
            tee.drain()
        sys.stdout, sys.stderr = self._saved
        self.line("-" * 72)
        self.line(footer)
        self._f.close()


_target_log: _TargetLog | None = None       # the log of the target this process runs now


def _open_log(opts: RunOptions, name: str, schema: str, alias: str, status: str) -> None:
    global _target_log
    if opts.log_dir is None:                # Celery / call_command: no files
        return
    _target_log = _TargetLog(Path(opts.log_dir) / name, {
        "schema": schema, "shard": alias, "status": status, "lease owner": owner(),
        "started": datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def _close_log(res: Result | None, secs: float) -> None:
    global _target_log
    if _target_log is None:
        return
    if res is None:
        footer = f"result: INTERRUPTED after {secs:.2f}s (see the run summary)"
    else:
        footer = (f"result: {res.kind} {res.reason} {secs:.2f}s — {res.msg}; invalid indexes: "
                  f"{', '.join(res.invalid_indexes) or 'none'}")
    log, _target_log = _target_log, None
    log.close(footer)


def _note_exc(e: BaseException) -> None:
    """The full traceback goes to the target's file only; the screen gets the Result."""
    if _target_log is not None:
        _target_log.note("".join(traceback.format_exception(type(e), e, e.__traceback__)))


# ---------------------------------------------------------------------------
# migrate_tenant (§6.1–§6.3, §6.5)
# ---------------------------------------------------------------------------

def _shard_idle(alias: str) -> bool:
    """§6.5: no connection, or no statement running on it server-side. After a
    KeyboardInterrupt psycopg has already tried to cancel and waited for the query
    (psycopg/connection.py wait()), so a non-ACTIVE status means the backend is done.
    TransactionStatus ACTIVE is 1 in both psycopg 3 and psycopg2."""
    raw = connections[alias].connection
    if raw is None:
        return True
    try:
        return raw.info.transaction_status != 1
    except Exception:
        return True                     # closed/broken wrapper: nothing running through it


def _forget(t: Tenant) -> None:
    """Invalidate the tenant's resolve-cache entries — only after a CAS changed 1 row (I3)."""
    _fresh()
    resolve_cache.forget_tenant(t)


def _load(pk: int) -> Tenant:
    _fresh()
    return Tenant.objects.select_related("shard").get(pk=pk)


def _gate(t: Tenant, opts: RunOptions, r: Any) -> Result | None:
    """Statuses the migrator never touches (step 1 of §6.1; re-checked under the lease)."""
    if t.status == Tenant.Status.FAILED:
        return r("skipped", Reason.STATUS_FAILED, "status_failed: reset via admin FAILED→NEW")
    if t.status == Tenant.Status.MAINTENANCE and not opts.maintenance:
        return r("skipped", Reason.STATUS_MAINTENANCE,
                 "status_maintenance: migrate explicitly with -s <schema> --maintenance")
    return None


def _upgradable(t: Tenant, opts: RunOptions) -> bool:
    return t.status in UPGRADABLE or (t.status == Tenant.Status.MAINTENANCE and opts.maintenance)


def _cas_lost(t: Tenant, r: Any) -> Result:
    """A fenced CAS updated 0 rows: one SELECT says why; the status is not written (§4.4)."""
    _fresh()
    row = (Tenant.objects.filter(pk=t.pk)
           .annotate(mine=Exists(MigrationLease.objects.filter(key=t.pk, owner=owner())))
           .values_list("status", "mine").first())
    if row is None:
        return r("skipped", Reason.TENANT_DELETED, "tenant deleted during the run")
    status, mine = row
    if not mine:
        return r("skipped", Reason.LEASE_LOST, "lease taken over during the run; status not written")
    return r("skipped", Reason.STATUS_CHANGED, f"status changed concurrently (now {status!r})")


def _failure(e: BaseException, alias: str, schema: str, r: Any) -> Result:
    """Non-connection failure: close the shard first, then read INVALID indexes."""
    _close_shard(alias)
    reason = (Reason.LOCK_TIMEOUT if is_lock_timeout(e)
              else Reason.INCOMPLETE_AFTER_RUN if isinstance(e, Incomplete)
              else Reason.MIGRATION_ERROR)
    return r("failed", reason, f"{type(e).__name__}: {e}"[:500],
             invalid_indexes=invalid_indexes(alias, schema))


def migrate_tenant(pk: int, opts: RunOptions) -> Result:
    """Bring one tenant up to date under its lease. Returns a Result for every outcome an
    Exception can produce; a BaseException (KeyboardInterrupt) propagates after the
    lease rule of §6.5."""
    started = time.monotonic()
    res = None
    try:
        res = _migrate_tenant(pk, opts)
        res = res._replace(secs=round(time.monotonic() - started, 2))
    finally:
        _close_log(res, time.monotonic() - started)
    level = (logging.ERROR if res.kind == "failed"
             else logging.WARNING if res.kind == "skipped" else logging.INFO)
    # `msg` is a reserved LogRecord attribute, hence `detail`.
    logger.log(level, "%s %s on %s: %s", res.kind, res.schema, res.shard, res.msg or res.reason,
               extra={"schema": res.schema, "shard": res.shard, "kind": res.kind,
                      "reason": res.reason, "detail": res.msg, "secs": res.secs})
    return res


def _migrate_tenant(pk: int, opts: RunOptions) -> Result:
    # 0. Load. The shard alias is fixed here, once (§5.3).
    try:
        t = _load(pk)
    except Tenant.DoesNotExist:
        return Result("skipped", f"<pk {pk}>", "", Reason.TENANT_DELETED, "tenant deleted")
    except Exception as e:
        if is_conn_error(e):
            return Result("failed", f"<pk {pk}>", "default", Reason.CONN_ERROR,
                          f"default unreachable: {e}", conn_error="default")
        raise
    alias, schema = t.shard.alias, t.schema_name

    def r(kind: str, reason: str, msg: str = "", **kw: Any) -> Result:
        return Result(kind, schema, alias, reason, msg, **kw)

    _open_log(opts, tenant_log_name(schema, t.pk), schema, alias, t.status)
    if t.shard.is_default:
        return r("failed", Reason.DEFAULT_SHARD_ANOMALY,
                 f"tenant on the default shard ({alias!r}): clean()-bypass anomaly, not migrated")

    # 1. Operator-owned statuses.
    if (gated := _gate(t, opts, r)) is not None:
        return gated

    # 2. NOOP shortcut: no lease, no writes to default.
    if _upgradable(t, opts) and not opts.targeted:
        try:
            if pending_plan(t) == []:
                return r("noop", Reason.UP_TO_DATE, "up to date")
        except Exception as e:
            _note_exc(e)
            if is_conn_error(e):
                return r("failed", Reason.CONN_ERROR, f"shard unreachable: {e}", conn_error="shard")
            return _failure(e, alias, schema, r)

    # 3. Lease.
    try:
        held = lease_acquire(t.pk, alias)
    except ShardUnavailable as e:
        return r("failed", Reason.CONN_ERROR, f"shard unreachable: {e.__cause__}", conn_error="shard")
    except Exception as e:
        if is_conn_error(e):
            return r("failed", Reason.CONN_ERROR, f"default unreachable: {e}", conn_error="default")
        raise
    if held is not None:
        return r("skipped", Reason.LOCKED, held)

    # 4. Under the lease.
    finished = False
    try:
        try:
            t = _load(pk)                       # re-read: the status may have moved
        except Tenant.DoesNotExist:
            finished = True
            return r("skipped", Reason.TENANT_DELETED, "tenant deleted")
        except Exception as e:
            finished = True
            if is_conn_error(e):
                return r("failed", Reason.CONN_ERROR, f"default unreachable: {e}", conn_error="default")
            return r("failed", Reason.MIGRATION_ERROR, f"{type(e).__name__}: {e}"[:500])
        if (gated := _gate(t, opts, r)) is not None:
            res = gated
        elif t.status in PROVISIONABLE:
            res = _provision(t, opts, r)
        elif _upgradable(t, opts):
            res = _upgrade(t, opts, r)
        else:
            res = r("skipped", Reason.STATUS_CHANGED, f"status changed concurrently (now {t.status!r})")
        finished = True
        return res
    finally:
        idle = _shard_idle(alias)               # remember BEFORE closing (§6.5)
        _close_shard(alias)                     # m5: shard first, then the lease
        if finished or idle:
            try:
                lease_release(t.pk)
            except Exception:
                logger.warning("could not release lease %s (%s); it expires with its TTL",
                               t.pk, schema, exc_info=True)
        else:
            logger.warning("lease %s (%s) kept: shard backend may still be running; "
                           "migration_lease --release %s", t.pk, schema, schema)


def _provision(t: Tenant, opts: RunOptions, r: Any) -> Result:
    """NEW/PENDING → ACTIVE (§6.2). Called under the lease."""
    alias, schema = t.shard.alias, t.schema_name
    if opts.targeted:                           # handle() rejects this combination already
        return r("skipped", Reason.TARGETED, "targeted flags are not allowed for provisioning")
    resumed = t.status == Tenant.Status.PENDING
    side, claimed = "default", False
    try:
        if code_older_than_db():
            return r("skipped", Reason.CODE_OLDER_THAN_DB,
                     "code_older_than_db: a newer release has already migrated public")

        # M4: never adopt a foreign non-empty schema silently.
        side = "shard"
        if (t.status == Tenant.Status.NEW and t.previous_status != Tenant.Status.FAILED
                and schema_has_relations(alias, schema)):
            side = "default"
            msg = (f"schema_exists: schema {schema!r} on {alias!r} already has relations; "
                   f"check whose it is, then admin FAILED→NEW (with confirmation)")
            if not _fenced_cas(t.pk, (Tenant.Status.NEW,), status=Tenant.Status.FAILED,
                               previous_status=Tenant.Status.NEW, last_error=msg):
                return _cas_lost(t, r)
            _forget(t)
            return r("failed", Reason.SCHEMA_EXISTS, msg)

        # Claim. NEW → PENDING is not invalidated: both are 503 (I3).
        side = "default"
        if not _fenced_cas(t.pk, PROVISIONABLE, status=Tenant.Status.PENDING,
                           previous_status=Tenant.Status.NEW, last_error=""):
            return _cas_lost(t, r)
        claimed = True
        if resumed:
            logger.info("RESUME %s", schema, extra={"schema": schema, "shard": alias})

        side = "shard"
        with connections[alias].cursor() as cur:            # on the SHARD, not on default
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_schema(schema)}")
        _open_shard(alias, t.pk)
        run_migrations([], _upstream_options(opts, alias), "standard", schema)
        plan = pending_plan(t)
        if plan is MISSING:
            raise RuntimeError(f"schema {schema!r} disappeared from {alias!r} during provisioning")
        if plan:
            raise Incomplete(len(plan))

        side = "default"
        # Re-check right before going ACTIVE: an OLD worker that claimed the tenant before
        # a rollout, and finished its (old) plan after the new release migrated public,
        # would otherwise make it ACTIVE on this release's schema while the new code
        # serves it. Left PENDING: the Celery retry or the next migrate_schemas resumes
        # it with the new code (review 2026-10-13).
        if code_older_than_db():
            return r("skipped", Reason.CODE_OLDER_THAN_DB,
                     "code_older_than_db: a newer release migrated public during "
                     "provisioning; left PENDING for the new code to finish")
        if not _fenced_cas(t.pk, (Tenant.Status.PENDING,), status=Tenant.Status.ACTIVE,
                           previous_status=Tenant.Status.PENDING, last_error=""):
            return _cas_lost(t, r)
        _forget(t)
        return (r("ok", Reason.RESUMED, "provisioned (RESUMED)") if resumed
                else r("ok", Reason.APPLIED, "provisioned"))
    except Exception as e:
        _note_exc(e)
        if is_conn_error(e):                    # stays PENDING: resumable (Q3)
            return r("failed", Reason.CONN_ERROR, f"{side} connection lost: {e}"[:500],
                     conn_error=side)
        res = _failure(e, alias, schema, r)
        if claimed:
            tb = traceback.format_exc()[-4000:]
            try:
                if _fenced_cas(t.pk, (Tenant.Status.PENDING,), status=Tenant.Status.FAILED,
                               previous_status=Tenant.Status.PENDING, last_error=tb):
                    _forget(t)
                else:
                    return _cas_lost(t, r)._replace(msg=res.msg, invalid_indexes=res.invalid_indexes)
            except Exception as e2:
                if is_conn_error(e2):
                    return res._replace(reason=Reason.CONN_ERROR, conn_error="default")
                raise
        return res


def _upgrade(t: Tenant, opts: RunOptions, r: Any) -> Result:
    """ACTIVE/DEACTIVATED (or MAINTENANCE with --maintenance): migrate; the status is
    written only for schema_missing on ACTIVE/DEACTIVATED (I6). Called under the lease."""
    alias, schema = t.shard.alias, t.schema_name
    side = "shard"
    try:
        plan = pending_plan(t)
        if plan is MISSING:
            msg = f'schema "{schema}" missing on {alias} during upgrade'
            if t.status == Tenant.Status.MAINTENANCE:       # operator-owned: never written
                return r("failed", Reason.SCHEMA_MISSING, msg)
            side = "default"
            if not _fenced_cas(t.pk, (t.status,), status=Tenant.Status.FAILED,
                               previous_status=t.status, last_error=msg):
                return _cas_lost(t, r)
            _forget(t)
            return r("failed", Reason.SCHEMA_MISSING, msg)
        if plan == [] and not opts.targeted:                # finished by someone meanwhile
            return r("noop", Reason.UP_TO_DATE, "up to date")

        _open_shard(alias, t.pk)
        run_migrations([], _upstream_options(opts, alias), "standard", schema)
        if opts.targeted:
            remaining = pending_plan(t)
            n = len(remaining) if remaining is not MISSING else "?"
            return r("ok", Reason.TARGETED, f"targeted, remaining={n}")
        return r("ok", Reason.APPLIED, f"applied {len(plan)} migration(s)")
    except Exception as e:
        _note_exc(e)
        if is_conn_error(e):
            return r("failed", Reason.CONN_ERROR, f"{side} connection lost: {e}"[:500],
                     conn_error=side)
        return _failure(e, alias, schema, r)


# ---------------------------------------------------------------------------
# migrate_shared (§6.4)
# ---------------------------------------------------------------------------

def _lease_table_exists() -> bool:
    _fresh()
    with connections["default"].cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"public.{_LEASE_TABLE}"])
        return cur.fetchone()[0]


def migrate_shared(opts: RunOptions) -> Result:
    """The `--shared` stage: public on default, under lease key 0. On a fresh database (no
    lease table yet — the very first --shared) it runs without a lease: a permanent branch
    for first deployments, not a transition shim. Busy → failed at once (Q6)."""
    started = time.monotonic()
    res = None
    _open_log(opts, SHARED_LOG, "public", "default", "-")
    try:
        res = _migrate_shared(opts)
        res = res._replace(secs=round(time.monotonic() - started, 2))
    finally:
        _close_log(res, time.monotonic() - started)
    logger.log(logging.ERROR if res.kind == "failed" else logging.INFO,
               "%s public (shared): %s", res.kind, res.msg or res.reason,
               extra={"schema": "public", "shard": "default", "kind": res.kind,
                      "reason": res.reason, "detail": res.msg, "secs": res.secs})
    return res


def _migrate_shared(opts: RunOptions) -> Result:
    def r(kind: str, reason: str, msg: str = "", **kw: Any) -> Result:
        return Result(kind, "public", "default", reason, msg, **kw)

    try:
        leased = _lease_table_exists()
        if leased and (held := lease_acquire(0, "default")) is not None:
            return r("failed", Reason.LOCKED,
                     "shared stage busy: " + held.replace("locked by", "held by", 1))
    except Exception as e:
        if is_conn_error(e) or isinstance(e, ShardUnavailable):
            return r("failed", Reason.CONN_ERROR, f"default unreachable: {e}", conn_error="default")
        raise

    finished = False
    try:
        _open_shard("default", 0)
        run_migrations([], _upstream_options(opts, "default"), "standard", "public")
        finished = True
        return r("ok", Reason.APPLIED, "shared migrations applied")
    except Exception as e:
        finished = True
        _note_exc(e)
        if is_conn_error(e):
            return r("failed", Reason.CONN_ERROR, f"default connection lost: {e}"[:500],
                     conn_error="default")
        reason = Reason.LOCK_TIMEOUT if is_lock_timeout(e) else Reason.MIGRATION_ERROR
        return r("failed", reason, f"{type(e).__name__}: {e}"[:500])
    finally:
        idle = _shard_idle("default")
        _close_shard("default")
        if leased and (finished or idle):
            try:
                lease_release(0)
            except Exception:
                logger.warning("could not release the shared lease; it expires with its TTL",
                               exc_info=True)


# ---------------------------------------------------------------------------
# Snapshot after a hard stop or a broken pool (§9.7)
# ---------------------------------------------------------------------------

def snapshot(key: int) -> dict[str, Any]:
    """What the DB says about a target that was in flight when the run was cut off:
    ``{"status", "behind", "lease_owner"}`` (+ ``"error"`` if it could not be read).
    `behind` is the number of unapplied migrations, None when the schema is missing (or
    for public, key 0, where only the lease is read). Never raises an Exception."""
    snap: dict[str, Any] = {"status": None, "behind": None, "lease_owner": None}
    try:
        h = lease_holder(key)
        snap["lease_owner"] = h.owner if h is not None else None
        if key == 0:
            return snap
        t = _load(key)
        snap["status"] = t.status
        snap["behind"] = behind(t)
    except Tenant.DoesNotExist:
        snap["status"] = "deleted"
    except Exception as e:
        snap["error"] = f"{type(e).__name__}: {e}"[:300]
    return snap
