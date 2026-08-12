#!/usr/bin/env python
"""In-process verifier + load generator for the tenant-resolution cache.

Drives the REAL production code path — ShardAwareTenantMiddleware.get_tenant()
-> tenants.resolve_cache -> CACHES["tenant_resolve"] (Redis) + `default` DB — with
no HTTP / nginx / gunicorn in the way, so it isolates the caching layer itself.

WHERE TO RUN
    On a host INSIDE the VPC (same security group) that can reach the staging
    tenant_resolve Redis (db2) AND the staging `default` Postgres, with the app
    code + a STAGING settings_local.py importable. Do NOT run load ON a gunicorn
    host (it steals CPU from the workers and skews numbers); use a sibling box.
    The tenant_resolve Redis / `default` DB are VPC-private (settings.py:330) — the
    script cannot run "from the internet".

SAFETY (built for staging)
    * Creates ephemeral rows only under the `bench_` schema-name / `.loadtest.local`
      domain namespace, and deletes them again (unless --keep).
    * Inspection uses SCAN + INFO only — never KEYS / MONITOR / flushdb.
    * Invalidation is prefix-scoped (tenants.resolve_cache), never a raw flush.
    Still: it writes into the SHARED tenant_resolve Redis and `default` DB. Run it
    against staging, not production.

MODES
    inspect   Show Redis INFO stats (keyspace hit ratio), key count, and a sample
              of live `tres:*` keys classified as positive / negative / hold.
    verify    Single-threaded correctness proofs (uses query counting):
                - positive hit serves from cache with ZERO `default` queries
                - negative (miss) is cached and re-served with ZERO queries
                - the nx + tombstone hold protocol survives a racing populate
    load      Concurrent throughput benchmark over a mix of known (positive) and
              unknown (miss) hosts; reports req/s, latency percentiles, logical
              hit/miss counts, and the Redis server-side hit-ratio delta.
    seed      Create the bench tenants/domains and exit (for manual poking).
    cleanup   Delete all bench_ rows and forget their cached hosts.

USAGE
    python scripts/resolve_cache_bench.py inspect
    python scripts/resolve_cache_bench.py verify
    python scripts/resolve_cache_bench.py load --duration 20 --concurrency 32 \
        --known 200 --miss-ratio 0.2 --warm
    python scripts/resolve_cache_bench.py cleanup

Settings module defaults to tenants_back.settings; override with
DJANGO_SETTINGS_MODULE (e.g. a staging module) before running.
"""
import argparse
import os
import random
import sys
import threading
import time
from contextlib import contextmanager

# --- Django bootstrap -------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)          # .../tenants_back  (has manage.py)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tenants_back.settings")

import django  # noqa: E402

django.setup()

from django.db import connections  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from tenants.middleware import ShardAwareTenantMiddleware  # noqa: E402
from tenants.models import Domain, Shard, Tenant  # noqa: E402
from tenants.resolve_cache import resolve_cache  # noqa: E402
from tenants.resolve_markers import NEGATIVE, TOMBSTONE  # noqa: E402

# Bench namespace — everything the script creates/mutates lives under here so it
# can never touch real tenants and cleanup is a simple prefix match.
BENCH_SCHEMA_PREFIX = "bench_"
BENCH_SHARD_ALIAS = "bench_shard"
BENCH_DOMAIN_SUFFIX = ".loadtest.local"

# One middleware instance is enough — get_tenant() is stateless w.r.t. the request.
_MW = ShardAwareTenantMiddleware(get_response=lambda request: None)


# --- low-level helpers ------------------------------------------------------
def raw_client():
    """The underlying redis-py client for the tenant_resolve cache (write node)."""
    return resolve_cache.cache.client.get_client(write=True)


def require_redis():
    if not resolve_cache.redis_alive():
        sys.exit("FATAL: tenant_resolve Redis is not reachable "
                 f"(check LOCATION for CACHES['tenant_resolve']).")


def classify(host):
    """What is stored for `host` right now: positive | negative | hold | absent."""
    v = resolve_cache.cache.get(host)
    if v is None:
        return "absent"
    if v == NEGATIVE:
        return "negative"
    if v == TOMBSTONE:
        return "hold"
    if isinstance(v, dict):
        return "positive"
    return "unknown"


def resolve(host):
    """Run the real cached resolution. Returns (found: bool). Never raises for a
    plain miss — DoesNotExist is the expected 'no tenant' answer."""
    try:
        _MW.get_tenant(Domain, host)
        return True
    except Domain.DoesNotExist:
        return False


@contextmanager
def count_default_queries():
    """Count queries issued on the `default` connection inside the block.
    Reliable only single-threaded (CaptureQueriesContext is per-connection)."""
    with CaptureQueriesContext(connections["default"]) as ctx:
        yield ctx


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


# --- bench data -------------------------------------------------------------
def _bench_domain(i):
    return f"bench-{i:05d}{BENCH_DOMAIN_SUFFIX}"


def ensure_bench_data(n):
    """Make sure N bench tenants+domains exist in `default`. Returns their domains.

    auto_create_schema=False on Tenant, and the resolve path never opens the shard
    schema, so these rows are pure `default`-table inserts — no Postgres schema is
    provisioned. .create() bypasses model clean() (same as the mgmt commands)."""
    shard, _ = Shard.objects.get_or_create(
        alias=BENCH_SHARD_ALIAS,
        defaults={"name": "bench (load-test, not routable)",
                  "is_default": False, "is_active": True},
    )
    existing = set(
        Domain.objects.filter(domain__endswith=BENCH_DOMAIN_SUFFIX)
        .values_list("domain", flat=True)
    )
    want = [_bench_domain(i) for i in range(n)]
    missing = [d for d in want if d not in existing]
    for d in missing:
        i = d.split("-", 1)[1].split(".", 1)[0]
        schema = f"{BENCH_SCHEMA_PREFIX}{i}"
        tenant, _ = Tenant.objects.get_or_create(
            schema_name=schema,
            defaults={"company_name": f"Bench {i}", "shard": shard,
                      "status": Tenant.Status.ACTIVE},
        )
        Domain.objects.get_or_create(domain=d, defaults={"tenant": tenant, "is_primary": True})
    if missing:
        print(f"seeded {len(missing)} new bench domain(s) (total requested: {n})")
    return want


def _sweep_bench_cache_keys():
    """Delete every cached tres key in the bench domain namespace — including the
    negative miss-markers (missing-*/uniq-*) that have no DB row. A bench-only
    artifact, so a direct per-key delete (no hold marker) is fine.

    A single SCAN can skip keys while others are expiring/rehashing, so gather
    into a set over a few passes until no new keys appear (short-TTL negatives that
    expire meanwhile just drop out — deleting them would be a no-op anyway)."""
    c = raw_client()
    physical_prefix = f"{resolve_cache.cache.key_prefix}:{resolve_cache.cache.version}:"
    pattern = f"{physical_prefix}*{BENCH_DOMAIN_SUFFIX}"
    found = set()
    for _ in range(5):
        batch = {(k.decode() if isinstance(k, (bytes, bytearray)) else k)
                 for k in c.scan_iter(match=pattern, count=1000)}
        if batch <= found:
            break
        found |= batch
    for key in found:
        resolve_cache.cache.delete(key[len(physical_prefix):])
    return len(found)


def cleanup_bench_data():
    # Delete DB rows FIRST: Domain.delete() fires the post_delete invalidation signal
    # (tenants.signals) which writes a fresh hold/tombstone marker per host. Sweeping
    # the cache AFTER therefore also removes those signal-written markers — sweeping
    # first would leave exactly `known` tombstones behind (they self-expire in
    # TENANT_RESOLVE_HOLD_SECONDS, but we clean fully).
    dn, _ = Domain.objects.filter(domain__endswith=BENCH_DOMAIN_SUFFIX).delete()
    tn, _ = Tenant.objects.filter(schema_name__startswith=BENCH_SCHEMA_PREFIX).delete()
    sn = 0
    if not Tenant.objects.filter(shard__alias=BENCH_SHARD_ALIAS).exists():
        sn, _ = Shard.objects.filter(alias=BENCH_SHARD_ALIAS).delete()
    swept = _sweep_bench_cache_keys()
    print(f"cleanup: removed {dn} domain row(s), {tn} tenant row(s), {sn} shard row(s); "
          f"deleted {swept} cached host key(s)")


# --- INFO stats -------------------------------------------------------------
def info_stats():
    c = raw_client()
    info = c.info("stats")
    hits = int(info.get("keyspace_hits", 0))
    misses = int(info.get("keyspace_misses", 0))
    return {"hits": hits, "misses": misses}


def count_tres_keys(sample=0):
    """Count (and optionally sample) live keys in the tenant_resolve namespace via
    SCAN — never KEYS. Returns (count, [sample logical hosts])."""
    c = raw_client()
    prefix = resolve_cache.cache.key_prefix
    version = resolve_cache.cache.version
    physical_prefix = f"{prefix}:{version}:"          # django_redis: prefix:version:key
    pattern = f"{physical_prefix}*"
    count = 0
    sampled = []
    for k in c.scan_iter(match=pattern, count=500):
        count += 1
        if len(sampled) < sample:
            key = k.decode() if isinstance(k, (bytes, bytearray)) else k
            sampled.append(key[len(physical_prefix):])
    return count, sampled


# --- modes ------------------------------------------------------------------
def mode_inspect(args):
    require_redis()
    st = info_stats()
    total = st["hits"] + st["misses"]
    ratio = (st["hits"] / total * 100.0) if total else 0.0
    print("== tenant_resolve Redis ==")
    print(f"  LOCATION prefix : {resolve_cache.cache.key_prefix}:{resolve_cache.cache.version}:")
    print(f"  keyspace_hits   : {st['hits']}")
    print(f"  keyspace_misses : {st['misses']}")
    print(f"  server hit ratio: {ratio:.1f}%   (cumulative, since Redis start)")
    count, sample = count_tres_keys(sample=args.sample)
    print(f"  live tres keys  : {count}")
    if sample:
        print(f"  sample (up to {args.sample}):")
        for host in sample:
            print(f"    [{classify(host):8}] ttl={resolve_cache.cache.ttl(host)!s:>6}  {host}")


def _check(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    return ok


def mode_verify(args):
    require_redis()
    if not resolve_cache.enabled:
        sys.exit("resolve_cache is disabled (both TTLs are 0) — nothing to verify.")
    domains = ensure_bench_data(max(1, args.known))
    known = domains[0]
    unknown = f"nope-{known}"       # guaranteed absent from DB
    ok = True
    print("== verify: positive (success) caching ==")
    resolve_cache.cache.delete(known)
    with count_default_queries() as q1:
        found1 = resolve(known)
    fill_q = len(q1)
    with count_default_queries() as q2:
        found2 = resolve(known)
    hit_q = len(q2)
    ok &= _check("first resolve is a DB fill", found1 and fill_q >= 1, f"{fill_q} default queries")
    ok &= _check("stored as positive snapshot", classify(known) == "positive",
                 f"ttl={resolve_cache.cache.ttl(known)}")
    ok &= _check("second resolve serves from cache with ZERO default queries",
                 found2 and hit_q == 0, f"{hit_q} default queries")

    print("== verify: negative (miss) caching ==")
    resolve_cache.cache.delete(unknown)
    with count_default_queries() as q3:
        found3 = resolve(unknown)
    miss_fill_q = len(q3)
    with count_default_queries() as q4:
        found4 = resolve(unknown)
    miss_hit_q = len(q4)
    ok &= _check("first miss hits the DB", (not found3) and miss_fill_q >= 1,
                 f"{miss_fill_q} default queries")
    ok &= _check("stored as NEGATIVE marker", classify(unknown) == "negative",
                 f"ttl={resolve_cache.cache.ttl(unknown)}")
    ok &= _check("second miss served from cache with ZERO default queries",
                 (not found4) and miss_hit_q == 0, f"{miss_hit_q} default queries")

    print("== verify: nx + tombstone hold protocol ==")
    # Warm, then invalidate -> a hold/tombstone marker is written. A racing populate
    # (store uses nx=True) must NOT overwrite the marker, so a stale snapshot can't
    # resurrect during the hold window.
    resolve_cache.cache.delete(known)
    resolve(known)                                   # positive again
    resolve_cache.forget_host(known)                 # -> TOMBSTONE (autocommit: runs now)
    tomb_ok = classify(known) == "hold"
    ok &= _check("invalidation writes a hold/tombstone marker", tomb_ok,
                 f"state={classify(known)}")
    with count_default_queries() as q5:
        resolve(known)                               # resolves DB-direct; store() nx can't win
    ok &= _check("resolve during hold goes to DB (treated as miss)", len(q5) >= 1,
                 f"{len(q5)} default queries")
    ok &= _check("hold marker NOT overwritten by the racing populate",
                 classify(known) == "hold", f"state={classify(known)}")

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
    if not args.keep:
        cleanup_bench_data()
    sys.exit(0 if ok else 1)


def mode_load(args):
    require_redis()
    if not resolve_cache.enabled:
        sys.exit("resolve_cache is disabled (both TTLs are 0) — load test is meaningless.")
    known = ensure_bench_data(args.known)
    # Bounded pool of unknown hosts. Reused -> exercises the NEGATIVE-cache hit path.
    # With --unique-misses each miss is a fresh host -> exercises the miss FILL path.
    miss_pool = [f"missing-{i:06d}{BENCH_DOMAIN_SUFFIX}" for i in range(max(1, args.known))]

    if args.reset:
        for d in known:
            resolve_cache.cache.delete(d)
        print(f"reset: cleared {len(known)} positive entries (cold-fill test)")
    if args.warm:
        n = resolve_cache.warm(force=True)
        print(f"warm: preloaded {n} positive entries")

    stop = threading.Event()
    deadline = None
    remaining = None
    lock = threading.Lock()
    if args.duration:
        deadline = time.perf_counter() + args.duration
    else:
        remaining = [args.requests]

    latencies = []
    counts = {"pos_found": 0, "miss": 0, "errors": 0}
    rnd_seed = 0

    def take_work():
        if deadline is not None:
            return time.perf_counter() < deadline and not stop.is_set()
        with lock:
            if remaining[0] <= 0 or stop.is_set():
                return False
            remaining[0] -= 1
            return True

    def worker(wid):
        rng = random.Random(1000 + wid)          # deterministic, no Date/rand-at-import
        local_lat = []
        local = {"pos_found": 0, "miss": 0, "errors": 0}
        try:
            while take_work():
                if rng.random() < args.miss_ratio:
                    if args.unique_misses:
                        host = f"uniq-{wid}-{local['miss']}-{rng.randint(0, 1_000_000)}{BENCH_DOMAIN_SUFFIX}"
                    else:
                        host = rng.choice(miss_pool)
                    expect_found = False
                else:
                    host = rng.choice(known)
                    expect_found = True
                t0 = time.perf_counter()
                try:
                    found = resolve(host)
                except Exception:                # fail-open path should swallow cache errors
                    local["errors"] += 1
                    continue
                local_lat.append((time.perf_counter() - t0) * 1000.0)
                if found:
                    local["pos_found"] += 1
                elif not expect_found:
                    local["miss"] += 1
                else:
                    local["errors"] += 1         # known host failed to resolve — unexpected
        finally:
            connections.close_all()              # release this thread's DB conn
            with lock:
                latencies.extend(local_lat)
                for k in counts:
                    counts[k] += local[k]

    before = info_stats()
    t_start = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(args.concurrency)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join()
    wall = time.perf_counter() - t_start
    after = info_stats()

    n = len(latencies)
    lat_sorted = sorted(latencies)
    d_hits = after["hits"] - before["hits"]
    d_miss = after["misses"] - before["misses"]
    d_total = d_hits + d_miss
    server_ratio = (d_hits / d_total * 100.0) if d_total else 0.0
    total_reqs = counts["pos_found"] + counts["miss"] + counts["errors"]

    print("\n== load result ==")
    print(f"  concurrency        : {args.concurrency} threads")
    print(f"  miss ratio (target): {args.miss_ratio:.0%}   "
          f"({'unique' if args.unique_misses else 'pooled'} miss hosts)")
    print(f"  warm/reset         : warm={args.warm} reset={args.reset}")
    print(f"  wall time          : {wall:.2f}s")
    print(f"  requests           : {total_reqs}  "
          f"(positive={counts['pos_found']}, miss={counts['miss']}, errors={counts['errors']})")
    print(f"  throughput         : {total_reqs / wall:,.0f} req/s")
    if n:
        print(f"  latency ms         : p50={pct(lat_sorted,50):.3f}  p90={pct(lat_sorted,90):.3f}  "
              f"p99={pct(lat_sorted,99):.3f}  max={lat_sorted[-1]:.3f}")
    print("  -- Redis server (delta over this run) --")
    print(f"  keyspace_hits  +{d_hits}")
    print(f"  keyspace_misses+{d_miss}")
    print(f"  server hit ratio   : {server_ratio:.1f}%")
    live, _ = count_tres_keys()
    print(f"  live tres keys now : {live}")

    if not args.keep:
        cleanup_bench_data()


def mode_seed(args):
    require_redis()
    domains = ensure_bench_data(args.known)
    print(f"bench data ready: {len(domains)} domain(s), first = {domains[0]}")


def mode_cleanup(args):
    cleanup_bench_data()


# --- CLI --------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Tenant-resolution cache verifier + load tester")
    sub = p.add_subparsers(dest="mode", required=True)

    pi = sub.add_parser("inspect", help="Show Redis stats + a sample of live keys")
    pi.add_argument("--sample", type=int, default=10, help="How many keys to sample")
    pi.set_defaults(func=mode_inspect)

    pv = sub.add_parser("verify", help="Correctness proofs (hit/miss/tombstone)")
    pv.add_argument("--known", type=int, default=1, help="Bench domains to seed")
    pv.add_argument("--keep", action="store_true", help="Do not delete bench data after")
    pv.set_defaults(func=mode_verify)

    pl = sub.add_parser("load", help="Concurrent throughput benchmark")
    g = pl.add_mutually_exclusive_group()
    g.add_argument("--duration", type=float, help="Run for N seconds")
    g.add_argument("--requests", type=int, help="Run exactly N requests")
    pl.add_argument("--concurrency", type=int, default=16, help="Worker threads")
    pl.add_argument("--known", type=int, default=100, help="Distinct known (positive) hosts")
    pl.add_argument("--miss-ratio", type=float, default=0.2, help="Fraction of miss requests (0..1)")
    pl.add_argument("--warm", action="store_true", help="Preload positive entries first")
    pl.add_argument("--reset", action="store_true", help="Clear positive entries first (cold fill)")
    pl.add_argument("--unique-misses", action="store_true",
                    help="Each miss is a fresh host (tests miss FILL, not negative-cache hit)")
    pl.add_argument("--keep", action="store_true", help="Do not delete bench data after")
    pl.set_defaults(func=mode_load)

    ps = sub.add_parser("seed", help="Create bench data and exit")
    ps.add_argument("--known", type=int, default=100)
    ps.set_defaults(func=mode_seed)

    pc = sub.add_parser("cleanup", help="Delete all bench data + forget cached hosts")
    pc.set_defaults(func=mode_cleanup)
    return p


def main():
    args = build_parser().parse_args()
    if getattr(args, "mode", None) == "load" and not args.duration and not args.requests:
        args.duration = 10.0        # sensible default
    args.func(args)


if __name__ == "__main__":
    main()
