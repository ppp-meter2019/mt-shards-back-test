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
    inspect    Show Redis INFO stats (keyspace hit ratio), key count, and a sample
               of live `tres:*` keys classified as positive / negative / hold.
    verify     Single-threaded correctness proofs (uses query counting):
                 - positive hit serves from cache with ZERO `default` queries
                 - negative (miss) is cached and re-served with ZERO queries
                 - the nx + tombstone hold protocol survives a racing populate
    rebuild    Prove the SET + snapshots REBUILD on DB changes (WARM on): domain add →
               incremental SADD; delete → SREM + orphan-sweep; status flip → reconcile
               force-overwrites the cached snapshot.
    coldstart  Prove the cold-start idea (WARM+GATE): a cold Redis fails OPEN (valid hosts
               still served, no outage), then after warm the gate is active (known = cache
               hit 0 DB, unknown = hard reject 0 DB).
    gate       Prove GATE steady-state: unknown hosts rejected with ZERO `default` queries;
               known hosts cold-fill once then hit.
    compare    A/B DB-query load test: replay one request sequence with the cache OFF vs ON
               and count actual `default` queries each way — shows how many DB hits the
               cache eliminates (the optimization, in hard numbers).
    load       TWO concurrent passes over the same workload — no gate, then WARM+GATE —
               side by side: throughput, latency, errors, real DB queries and PEAK
               concurrent DB connections each way (shows the gate cutting DB pressure).
    seed       Create the bench tenants/domains and exit (for manual poking).
    cleanup    Delete all bench_ rows and forget their cached hosts.

USAGE
    python scripts/resolve_cache_bench.py verify
    python scripts/resolve_cache_bench.py rebuild
    python scripts/resolve_cache_bench.py coldstart
    python scripts/resolve_cache_bench.py gate --known 50 --requests 500 --miss-ratio 0.5
    python scripts/resolve_cache_bench.py compare --requests 2000 --known 50 --miss-ratio 0.3
    python scripts/resolve_cache_bench.py load --duration 20 --concurrency 32 \
        --known 200 --miss-ratio 0.5 --unique-misses
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
from tenants.resolver import HOSTS_KEY, host_registry, resolve_cache  # noqa: E402

# Bench namespace — everything the script creates/mutates lives under here so it
# can never touch real tenants and cleanup is a simple prefix match.
BENCH_SCHEMA_PREFIX = "bench_"
BENCH_SHARD_ALIAS = "bench_shard"
BENCH_DOMAIN_SUFFIX = ".loadtest.local"

# One middleware instance is enough — get_tenant() is stateless w.r.t. the request.
_MW = ShardAwareTenantMiddleware(get_response=lambda request: None)


# --- low-level helpers ------------------------------------------------------
def get_redis_raw_client():
    """The underlying redis-py client for the tenant_resolve cache (write node)."""
    return resolve_cache.get_redis_raw_client()


def require_redis():
    if not resolve_cache.redis_alive():
        sys.exit("FATAL: tenant_resolve Redis is not reachable "
                 f"(check LOCATION for CACHES['tenant_resolve']).")


def _snap(host):
    """Physical snapshot cache key for a host (goes through the resolver's namespacing)."""
    return resolve_cache._snap_key(host)


def classify(host):
    """What is stored for `host` right now: positive | negative | hold | absent | unknown.
    Delegates to the resolver's single classifier so the bench never re-implements the shapes."""
    K = resolve_cache._Kind
    kind = resolve_cache._classify(resolve_cache.cache.get(_snap(host)))
    return {K.MISS: "absent", K.NEG: "negative"}.get(kind, kind)   # HOLD/POSITIVE/UNKNOWN already match


def _is_member(host):
    """True iff `host` is currently in the gate SET (treg:hosts)."""
    return bool(get_redis_raw_client().sismember(HOSTS_KEY, host))


def _enable_warm_gate(gate=True):
    """Force WARM (+ optionally GATE) on for this process — read live by registry_cfg."""
    from django.conf import settings as dj
    dj.TENANT_REGISTRY = {"WARM_ENABLED": True, "GATE_ENABLED": gate}


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


def _add_bench_domain(i):
    """Create ONE extra bench tenant+domain at index `i` (fires the real post_save signals).
    Returns its domain."""
    shard = Shard.objects.get(alias=BENCH_SHARD_ALIAS)
    schema = f"{BENCH_SCHEMA_PREFIX}{i:05d}"
    tenant, _ = Tenant.objects.get_or_create(
        schema_name=schema,
        defaults={"company_name": f"Bench {i}", "shard": shard, "status": Tenant.Status.ACTIVE},
    )
    d = _bench_domain(i)
    Domain.objects.get_or_create(domain=d, defaults={"tenant": tenant, "is_primary": True})
    return d


def _sweep_bench_cache_keys():
    """Delete every cached tres key in the bench domain namespace — including the
    negative miss-markers (missing-*/uniq-*) that have no DB row. A bench-only
    artifact, so a direct per-key delete (no hold marker) is fine.

    A single SCAN can skip keys while others are expiring/rehashing, so gather
    into a set over a few passes until no new keys appear (short-TTL negatives that
    expire meanwhile just drop out — deleting them would be a no-op anyway)."""
    c = get_redis_raw_client()
    physical_prefix = resolve_cache._snapshot_key_prefix()   # single source of the key layout
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
    c = get_redis_raw_client()
    info = c.info("stats")
    hits = int(info.get("keyspace_hits", 0))
    misses = int(info.get("keyspace_misses", 0))
    return {"hits": hits, "misses": misses}


def count_tres_keys(sample=0):
    """Count (and optionally sample) live keys in the tenant_resolve namespace via
    SCAN — never KEYS. Returns (count, [sample logical hosts])."""
    count = 0
    sampled = []
    for host in resolve_cache.iter_snapshot_hosts():   # cache owns the key layout
        count += 1
        if len(sampled) < sample:
            sampled.append(host)
    return count, sampled


# --- modes ------------------------------------------------------------------
def mode_inspect(args):
    require_redis()
    st = info_stats()
    total = st["hits"] + st["misses"]
    ratio = (st["hits"] / total * 100.0) if total else 0.0
    print("== tenant_resolve Redis ==")
    print(f"  snapshot prefix : {resolve_cache._snapshot_key_prefix()}")
    print(f"  keyspace_hits   : {st['hits']}")
    print(f"  keyspace_misses : {st['misses']}")
    print(f"  server hit ratio: {ratio:.1f}%   (cumulative, since Redis start)")
    count, sample = count_tres_keys(sample=args.sample)
    print(f"  live tres keys  : {count}")
    if sample:
        print(f"  sample (up to {args.sample}):")
        for host in sample:
            print(f"    [{classify(host):8}] ttl={resolve_cache.cache.ttl(_snap(host))!s:>6}  {host}")


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
    resolve_cache.cache.delete(_snap(known))
    with count_default_queries() as q1:
        found1 = resolve(known)
    fill_q = len(q1)
    with count_default_queries() as q2:
        found2 = resolve(known)
    hit_q = len(q2)
    ok &= _check("first resolve is a DB fill", found1 and fill_q >= 1, f"{fill_q} default queries")
    ok &= _check("stored as positive snapshot", classify(known) == "positive",
                 f"ttl={resolve_cache.cache.ttl(_snap(known))}")
    ok &= _check("second resolve serves from cache with ZERO default queries",
                 found2 and hit_q == 0, f"{hit_q} default queries")

    print("== verify: negative (miss) caching ==")
    resolve_cache.cache.delete(_snap(unknown))
    with count_default_queries() as q3:
        found3 = resolve(unknown)
    miss_fill_q = len(q3)
    with count_default_queries() as q4:
        found4 = resolve(unknown)
    miss_hit_q = len(q4)
    ok &= _check("first miss hits the DB", (not found3) and miss_fill_q >= 1,
                 f"{miss_fill_q} default queries")
    ok &= _check("stored as NEGATIVE marker", classify(unknown) == "negative",
                 f"ttl={resolve_cache.cache.ttl(_snap(unknown))}")
    ok &= _check("second miss served from cache with ZERO default queries",
                 (not found4) and miss_hit_q == 0, f"{miss_hit_q} default queries")

    print("== verify: nx + tombstone hold protocol ==")
    # Warm, then invalidate -> a hold/tombstone marker is written. A racing populate
    # (store uses nx=True) must NOT overwrite the marker, so a stale snapshot can't
    # resurrect during the hold window.
    resolve_cache.cache.delete(_snap(known))
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


def _run_load_pass(args, known, miss_pool, *, gate_on):
    """Run ONE concurrent load pass and return its metrics. gate_on toggles WARM+GATE
    (builds the SET first); either way snapshots start cold (forget_all) for a fair fill
    pattern. Instruments real `default` DB queries + PEAK concurrency via execute_wrapper."""
    from django.conf import settings as dj

    resolve_cache.forget_all()                   # clean slate for this pass
    if gate_on:
        _enable_warm_gate(gate=True)
        host_registry.run_locked()               # build SET + warm KNOWN positives
    else:
        dj.TENANT_REGISTRY = {"WARM_ENABLED": False, "GATE_ENABLED": False}
        resolve_cache.warm(force=True)           # warm KNOWN positives (gate-off)
    # KNOWN hosts are pre-warmed → steady state; only MISS handling differs between passes,
    # so measured DB load / errors come from the flood, not a one-time known cold-fill herd.

    stop = threading.Event()
    lock = threading.Lock()
    deadline = time.perf_counter() + args.duration if args.duration else None
    remaining = None if args.duration else [args.requests]
    latencies = []
    counts = {"served": 0, "rejected": 0, "unexpected": 0, "exc": 0}
    exc_types = {}
    gauge_lock = threading.Lock()
    db_stat = {"queries": 0, "inflight": 0, "peak": 0}

    def db_gauge(execute, sql, params, many, context):
        with gauge_lock:
            db_stat["queries"] += 1
            db_stat["inflight"] += 1
            db_stat["peak"] = max(db_stat["peak"], db_stat["inflight"])
        try:
            return execute(sql, params, many, context)
        finally:
            with gauge_lock:
                db_stat["inflight"] -= 1

    def take_work():
        if deadline is not None:
            return time.perf_counter() < deadline and not stop.is_set()
        with lock:
            if remaining[0] <= 0 or stop.is_set():
                return False
            remaining[0] -= 1
            return True

    def worker(wid):
        rng = random.Random(1000 + wid)          # deterministic per worker
        local_lat = []
        local = {"served": 0, "rejected": 0, "unexpected": 0, "exc": 0}
        local_exc = {}
        try:
            with connections["default"].execute_wrapper(db_gauge):
                while take_work():
                    if rng.random() < args.miss_ratio:
                        if args.unique_misses:
                            host = f"uniq-{wid}-{local['rejected']}-{rng.randint(0, 1_000_000)}{BENCH_DOMAIN_SUFFIX}"
                        else:
                            host = rng.choice(miss_pool)
                        expect_found = False
                    else:
                        host = rng.choice(known)
                        expect_found = True
                    t0 = time.perf_counter()
                    try:
                        found = resolve(host)
                    except Exception as exc:      # resolve raised (e.g. DB pool timeout)
                        local["exc"] += 1
                        name = type(exc).__name__
                        local_exc[name] = local_exc.get(name, 0) + 1
                        continue
                    local_lat.append((time.perf_counter() - t0) * 1000.0)
                    if found:
                        local["served"] += 1
                    elif not expect_found:
                        local["rejected"] += 1
                    else:
                        local["unexpected"] += 1
        finally:
            connections.close_all()              # release this thread's DB conn
            with lock:
                latencies.extend(local_lat)
                for k in counts:
                    counts[k] += local[k]
                for name, cnt in local_exc.items():
                    exc_types[name] = exc_types.get(name, 0) + cnt

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

    d_hits = after["hits"] - before["hits"]
    d_miss = after["misses"] - before["misses"]
    d_total = d_hits + d_miss
    total = sum(counts.values())
    return {
        "gate_on": gate_on, "wall": wall, "total": total,
        "throughput": total / wall if wall else 0.0,
        "counts": dict(counts), "exc_types": dict(exc_types),
        "lat": sorted(latencies),
        "db_queries": db_stat["queries"], "db_peak": db_stat["peak"],
        "d_hits": d_hits, "d_miss": d_miss,
        "offload": (d_hits / d_total * 100.0) if d_total else 0.0,
    }


def _fmt_pass(r):
    c, lat = r["counts"], r["lat"]
    lat_s = (f"p50={pct(lat,50):.1f} p90={pct(lat,90):.1f} p99={pct(lat,99):.1f} max={lat[-1]:.0f}"
             if lat else "n/a")
    top = ", ".join(f"{k}×{v}" for k, v in sorted(r["exc_types"].items(), key=lambda kv: -kv[1])) or "—"
    err_rate = (c["exc"] / r["total"] * 100.0) if r["total"] else 0.0
    print(f"\n--- {'WARM+GATE' if r['gate_on'] else 'no gate (cache only)'} ---")
    print(f"  throughput      : {r['throughput']:,.0f} req/s   ({r['total']} reqs in {r['wall']:.1f}s)")
    line = f"  requests        : served={c['served']}  rejected={c['rejected']}"
    if c["unexpected"]:
        line += f"  unexpected={c['unexpected']}"
    line += f"  errors={c['exc']} ({err_rate:.2f}%)" + (f" [{top}]" if c["exc"] else "")
    print(line)
    print(f"  latency ms      : {lat_s}")
    print(f"  DB queries      : {r['db_queries']}   (resolves that missed cache and hit the DB)")
    print(f"  peak concurrent : {r['db_peak']}   ← max DB queries in flight at once")
    print(f"  cache offload   : {r['offload']:.1f}%")


def mode_load(args):
    """Two concurrent passes over the SAME workload — WITHOUT the gate, then WITH WARM+GATE —
    so the gate's effect on efficiency and on peak DB connections is visible side by side.
    Each pass measures real `default` DB queries + peak concurrency (execute_wrapper)."""
    require_redis()
    if not resolve_cache.enabled:
        sys.exit("resolve_cache is disabled (both TTLs are 0) — load test is meaningless.")
    from unittest import mock

    from django.conf import settings as dj

    known = ensure_bench_data(args.known)
    miss_pool = [f"missing-{i:06d}{BENCH_DOMAIN_SUFFIX}" for i in range(max(1, args.known))]

    with mock.patch.object(host_registry, "trigger_warm", lambda: None):
        off = _run_load_pass(args, known, miss_pool, gate_on=False)
        gate = _run_load_pass(args, known, miss_pool, gate_on=True)
        # back to safe defaults so cleanup's delete signals don't enqueue on a broker
        dj.TENANT_REGISTRY = {"WARM_ENABLED": False, "GATE_ENABLED": False}
        get_redis_raw_client().delete(HOSTS_KEY)
        if not args.keep:
            cleanup_bench_data()

    bar = "=" * 64
    print(f"\n{bar}\nLOAD TEST — cache-only vs WARM+GATE\n{bar}")
    print(f"{args.concurrency} threads | {'unique' if args.unique_misses else 'pooled'} misses | "
          f"miss-ratio {args.miss_ratio:.0%} | "
          + (f"{args.duration:.0f}s each pass" if args.duration else f"{args.requests} reqs each pass"))
    print("Same workload both passes; KNOWN hosts pre-warmed, so only MISS handling differs.")
    print("errors = OperationalError when concurrent DB fills outrun the `default` pool. Compare")
    print("the error RATE (per request), NOT the raw count — the gate pass handles far more reqs.")
    _fmt_pass(off)
    _fmt_pass(gate)

    off_er = off["counts"]["exc"] / off["total"] * 100 if off["total"] else 0.0
    gate_er = gate["counts"]["exc"] / gate["total"] * 100 if gate["total"] else 0.0
    print(f"\n{bar}\nEFFECT OF THE GATE (no-gate → gate)")
    print(f"  DB queries      : {off['db_queries']} → {gate['db_queries']}   "
          f"({off['db_queries'] - gate['db_queries']} fewer — the flood no longer hits the DB)")
    print(f"  peak concurrent : {off['db_peak']} → {gate['db_peak']}   (lower = less pool pressure)")
    print(f"  error rate      : {off_er:.2f}% → {gate_er:.2f}%   "
          f"(raw {off['counts']['exc']} vs {gate['counts']['exc']}, over different volumes)")
    print(f"  throughput      : {off['throughput']:,.0f} → {gate['throughput']:,.0f} req/s")
    print("  Unknown hosts are rejected in Redis under the gate → they never reach the DB, so")
    print("  the DB load and error RATE from an unknown-host flood collapse toward zero.")
    print(bar)


def mode_seed(args):
    require_redis()
    domains = ensure_bench_data(args.known)
    print(f"bench data ready: {len(domains)} domain(s), first = {domains[0]}")


def mode_cleanup(args):
    cleanup_bench_data()


def mode_gate(args):
    """Prove the gate: with WARM+GATE on and the SET built, UNKNOWN-host requests are
    rejected with ZERO `default` queries; known hosts cold-fill once then hit.

    Single-threaded on purpose — CaptureQueriesContext is authoritative per connection.
    Forces both flags on and neutralizes the celery trigger so the proof is
    broker-independent (production uses the real host_registry.trigger_warm)."""
    require_redis()
    from unittest import mock

    _enable_warm_gate(gate=True)
    dbq_unknown = dbq_known = n_unknown = n_known = leaked = 0
    with mock.patch.object(host_registry, "trigger_warm", lambda: None):
        known = ensure_bench_data(args.known)
        for d in known:                                # clear create-time holds so warm can populate
            resolve_cache.cache.delete(_snap(d))
        n = host_registry.run_locked()                 # build treg:hosts + warm positives
        print(f"reconcile: warmed {n} positive(s); {HOSTS_KEY} members = {get_redis_raw_client().scard(HOSTS_KEY)}")

        rng = random.Random(0)
        for i in range(args.requests):
            if rng.random() < args.miss_ratio:
                host = f"uniq-{i}-{rng.randint(0, 1_000_000_000)}{BENCH_DOMAIN_SUFFIX}"  # fresh unknown
                with count_default_queries() as q:
                    found = resolve(host)
                dbq_unknown += len(q); n_unknown += 1
                leaked += 1 if found else 0
            else:
                host = rng.choice(known)
                with count_default_queries() as q:
                    resolve(host)
                dbq_known += len(q); n_known += 1

        print("\n== gate result (single-threaded, CaptureQueriesContext) ==")
        print(f"  unknown reqs : {n_unknown}  default queries = {dbq_unknown}   "
              f"[{'PASS' if dbq_unknown == 0 else 'FAIL'} — expect 0: gate rejects without DB]")
        print(f"  known reqs   : {n_known}  default queries = {dbq_known}   "
              f"(cold-fill once per distinct host, then 0)")
        print(f"  leaked unknown resolves: {leaked}  [{'PASS' if leaked == 0 else 'FAIL'} — expect 0]")

        if not args.keep:
            cleanup_bench_data()                       # inside patch: delete signals won't enqueue
    sys.exit(0 if (dbq_unknown == 0 and leaked == 0) else 1)


def mode_rebuild(args):
    """Prove the cache REBUILDS on DB changes (WARM on; broker trigger neutralized):
      * add domain    → instant member via incremental SADD (post_save signal),
                        reconcile writes its positive snapshot;
      * delete domain → instant non-member via incremental SREM (post_delete signal),
                        reconcile + orphan-sweep drop the stale positive;
      * status change → reconcile force-overwrites the cached snapshot with the new status.
    Single-threaded; CaptureQueriesContext is authoritative per connection."""
    require_redis()
    from unittest import mock

    _enable_warm_gate(gate=True)
    c = get_redis_raw_client()
    ok = True
    with mock.patch.object(host_registry, "trigger_warm", lambda: None):
        known = ensure_bench_data(args.known)
        for d in known:                                    # clear create-holds so warm can populate
            resolve_cache.cache.delete(_snap(d))
        host_registry.run_locked()
        base = c.scard(HOSTS_KEY)
        ok &= _check("baseline: SET built from DB", base == len(known),
                     f"{base} members == {len(known)} domains")

        print("== change: ADD a domain ==")
        newd = _add_bench_domain(args.known)               # index N (one past the seeded range)
        ok &= _check("add → instant member (incremental SADD via signal)", _is_member(newd),
                     f"member={_is_member(newd)}")
        host_registry.run_locked()
        ok &= _check("add → reconcile writes the positive snapshot",
                     classify(newd) == "positive",
                     f"state={classify(newd)}, scard={c.scard(HOSTS_KEY)}")

        print("== change: DELETE a domain ==")
        gone = known[0]
        Domain.objects.filter(domain=gone).delete()
        ok &= _check("delete → instant non-member (incremental SREM via signal)",
                     not _is_member(gone), f"member={_is_member(gone)}")
        host_registry.run_locked()
        ok &= _check("delete → reconcile + orphan-sweep drop the stale positive",
                     classify(gone) != "positive", f"state={classify(gone)}")

        print("== change: STATUS flip (signal-bypassing-safe: reconcile force-overwrites) ==")
        live = known[1]
        t = Domain.objects.get(domain=live).tenant
        t.status = Tenant.Status.DEACTIVATED
        t.save(update_fields=["status"])
        host_registry.run_locked()
        snap = resolve_cache.get_snapshot(live)
        got = getattr(snap, "status", None)
        ok &= _check("status change → cached snapshot refreshed by reconcile",
                     got == Tenant.Status.DEACTIVATED, f"snapshot status={got}")

        print("\nRESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
        if not args.keep:
            cleanup_bench_data()
    sys.exit(0 if ok else 1)


def mode_coldstart(args):
    """Prove the COLD-START idea. With a cold Redis (no SET, no snapshots) under WARM+GATE:
      * a valid host is STILL served (fail-open) — no cold-start outage, nobody 404'd because
        Redis is empty; an unknown host is checked via the DB (fail-open), NOT a no-DB reject;
      then after warm (reconcile builds the SET+snapshots) the gate is fully active:
      * known host  → served from cache with ZERO default queries;
      * unknown host → HARD-REJECTED with ZERO default queries.
    Single-threaded; CaptureQueriesContext is authoritative per connection."""
    require_redis()
    from unittest import mock

    _enable_warm_gate(gate=True)
    c = get_redis_raw_client()
    ok = True
    with mock.patch.object(host_registry, "trigger_warm", lambda: None):
        known = ensure_bench_data(args.known)
        k = known[0]
        c.delete(HOSTS_KEY)                                # go COLD: drop the SET ...
        resolve_cache.forget_all()                         # ... and flush all snapshots
        print(f"cold: EXISTS(hosts)={c.exists(HOSTS_KEY)}, snapshots={count_tres_keys()[0]}")

        print("== cold phase (SET absent → fail-open, NO outage) ==")
        with count_default_queries() as q1:
            found_k = resolve(k)
        ok &= _check("cold: known host STILL resolves (fail-open, not 404'd)", found_k,
                     f"{len(q1)} default queries (a DB fill is expected here)")
        with count_default_queries() as q2:
            found_u = resolve(f"cold-unknown{BENCH_DOMAIN_SUFFIX}")
        ok &= _check("cold: unknown host checked via DB (fail-open, not a no-DB hard reject)",
                     (not found_u) and len(q2) >= 1, f"{len(q2)} default queries")

        print("== warm (reconcile builds SET + snapshots) ==")
        n = host_registry.run_locked()
        print(f"warm: {c.scard(HOSTS_KEY)} SET members, {n} positive(s)")

        print("== warm phase (gate active) ==")
        with count_default_queries() as q3:
            found_k2 = resolve(k)
        ok &= _check("warm: known host served from cache, ZERO default queries",
                     found_k2 and len(q3) == 0, f"{len(q3)} default queries")
        with count_default_queries() as q4:                # fresh unknown → no negative cached
            found_u2 = resolve(f"warm-unknown{BENCH_DOMAIN_SUFFIX}")
        ok &= _check("warm: unknown host HARD-REJECTED with ZERO default queries",
                     (not found_u2) and len(q4) == 0, f"{len(q4)} default queries")

        print("\nRESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
        if not args.keep:
            cleanup_bench_data()
    sys.exit(0 if ok else 1)


def mode_compare(args):
    """A/B/C DB-query load test: replay ONE request sequence three ways and count actual
    `default` queries each — showing what the cache, and then the gate, eliminate:
      OFF          cache disabled → every request hits the DB (baseline);
      ON           positive + negative cache, gate off → 1 cold-fill per DISTINCT host;
      WARM+GATE    unknown hosts are hard-rejected with ZERO DB queries → only distinct
                   KNOWN hosts cold-fill (unknowns cost nothing — the anti-cache-penetration win).
    With --unique-misses each miss is a fresh host (DoS shape): ON pays 1 DB per unique miss,
    WARM+GATE pays 0. Single-threaded so CaptureQueriesContext is authoritative; the query
    RATIO is concurrency-independent (use `load` for raw throughput)."""
    require_redis()
    from unittest import mock

    from django.conf import settings as dj

    known = ensure_bench_data(args.known)
    miss_pool = [f"missing-{i:06d}{BENCH_DOMAIN_SUFFIX}" for i in range(max(1, args.known))]
    rng = random.Random(0)
    seq, uniq = [], 0                                    # ONE fixed sequence, replayed for all arms
    for _ in range(args.requests):
        if rng.random() < args.miss_ratio:
            if args.unique_misses:
                seq.append(f"uniq-{uniq}{BENCH_DOMAIN_SUFFIX}"); uniq += 1
            else:
                seq.append(rng.choice(miss_pool))
        else:
            seq.append(rng.choice(known))
    distinct = len(set(seq))

    def run():
        resolve_cache.forget_all()                       # cold snapshots (SET, if any, survives)
        found = 0
        t0 = time.perf_counter()
        with count_default_queries() as q:
            for h in seq:
                if resolve(h):
                    found += 1
        return len(q), found, time.perf_counter() - t0

    # --- OFF: both TTLs 0, no WARM/GATE → resolve() goes straight to the DB ---
    dj.TENANT_RESOLVE = {"POSITIVE_CACHE_SECONDS": 0, "MISS_CACHE_SECONDS": 0}
    dj.TENANT_REGISTRY = {"WARM_ENABLED": False, "GATE_ENABLED": False}
    off_q, off_found, off_wall = run()

    # --- ON: positive + negative caching; gate off ---
    dj.TENANT_RESOLVE = {"POSITIVE_CACHE_SECONDS": 3600, "MISS_CACHE_SECONDS": 60}
    on_q, on_found, on_wall = run()

    # --- WARM+GATE: SET built; unknowns hard-rejected with ZERO DB ---
    _enable_warm_gate(gate=True)
    with mock.patch.object(host_registry, "trigger_warm", lambda: None):
        host_registry.run_locked()                       # build treg:hosts (run() then clears positives)
        gate_q, gate_found, gate_wall = run()

    R = args.requests
    sane = off_found == on_found == gate_found
    known_set = set(known)
    distinct_known = len({h for h in seq if h in known_set})
    distinct_miss = distinct - distinct_known
    miss_reqs = R - off_found

    def pct(x):
        return (1 - x / off_q) * 100 if off_q else 0.0

    bar = "=" * 64
    print(f"\n{bar}\nCACHE / GATE A/B/C COMPARISON\n{bar}")
    print("Replayed the SAME request sequence through three configurations and counted how")
    print("many requests reached the `default` DB. FEWER DB queries = the cache/gate helping.\n")

    print("Workload:")
    print(f"  {R} requests over {distinct} distinct hosts")
    print(f"    known  (in DB) : {off_found} requests across {distinct_known} hosts  → should resolve")
    print(f"    unknown (miss) : {miss_reqs} requests across {distinct_miss} hosts  "
          f"({'each UNIQUE' if args.unique_misses else 'reused pool'}) → should NOT resolve")
    print(f"  same answer every run (found off={off_found} on={on_found} gate={gate_found}) "
          f"[{'OK' if sane else 'MISMATCH'}] — only the DB cost differs.\n")

    print("DB queries to `default` (lower is better):\n")
    print(f"  [1] CACHE OFF          {off_q:>6}   ({off_q / R:.2f}/req)   baseline")
    print( "      no cache at all — every request goes to the DB.\n")
    print(f"  [2] CACHE ON, no gate  {on_q:>6}   ({on_q / R:.2f}/req)   {pct(on_q):.1f}% fewer than [1]")
    if args.unique_misses:
        print( "      positive+negative cache, but each DISTINCT host still costs one DB fill.")
        print( "      Unique unknowns are never repeated → one DB query PER unknown: the cache")
        print( "      is \"penetrated\" and degrades toward no-cache under a flood.\n")
    else:
        print( "      positive+negative cache: repeated hosts (known AND miss) are served from")
        print( "      cache; only the first hit of each distinct host costs a DB fill.\n")
    print(f"  [3] WARM + GATE        {gate_q:>6}   ({gate_q / R:.2f}/req)   {pct(gate_q):.1f}% fewer than [1]")
    print( "      the gate rejects unknown hosts from a Redis SET WITHOUT touching the DB, so")
    print(f"      only the {distinct_known} known hosts ever cold-fill. {on_q - gate_q} fewer DB queries than [2]")
    print( "      — unknown hosts cost ZERO DB, no matter how many or how unique.\n")

    print(f"Wall time:  OFF {off_wall:.2f}s   ON {on_wall:.2f}s   GATE {gate_wall:.2f}s   "
          "(fewer DB round-trips ⇒ faster)")

    ok = sane and gate_q <= on_q < off_q
    print(f"\n{bar}")
    if ok and args.unique_misses:
        print("VERDICT: PASS — under an unknown-host flood a plain cache degrades toward")
        print(f"         no-cache ({on_q}≈{off_q}); only the GATE keeps DB load flat ({gate_q}).")
    elif ok:
        print("VERDICT: PASS — the cache cuts DB load; the gate additionally makes unknown")
        print("         hosts cost zero DB (run with --unique-misses to see the DoS case).")
    else:
        print("VERDICT: FAIL")
    print(bar)
    # Turn WARM/GATE back OFF so cleanup's delete signals don't enqueue a reconcile onto a
    # (possibly absent) broker, and drop the SET the gate arm built.
    dj.TENANT_REGISTRY = {"WARM_ENABLED": False, "GATE_ENABLED": False}
    get_redis_raw_client().delete(HOSTS_KEY)
    if not args.keep:
        cleanup_bench_data()
    sys.exit(0 if ok else 1)


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

    pl = sub.add_parser("load", help="Two passes (no-gate vs WARM+GATE): throughput, errors, peak DB conns")
    g = pl.add_mutually_exclusive_group()
    g.add_argument("--duration", type=float, help="Run for N seconds")
    g.add_argument("--requests", type=int, help="Run exactly N requests")
    pl.add_argument("--concurrency", type=int, default=16, help="Worker threads")
    pl.add_argument("--known", type=int, default=100, help="Distinct known (positive) hosts")
    pl.add_argument("--miss-ratio", type=float, default=0.2, help="Fraction of miss requests (0..1)")
    pl.add_argument("--unique-misses", action="store_true",
                    help="Each miss is a fresh host (tests miss FILL, not negative-cache hit)")
    pl.add_argument("--keep", action="store_true", help="Do not delete bench data after")
    pl.set_defaults(func=mode_load)

    ps = sub.add_parser("seed", help="Create bench data and exit")
    ps.add_argument("--known", type=int, default=100)
    ps.set_defaults(func=mode_seed)

    pc = sub.add_parser("cleanup", help="Delete all bench data + forget cached hosts")
    pc.set_defaults(func=mode_cleanup)

    pg = sub.add_parser("gate", help="Prove GATE: unknown hosts rejected with ZERO default queries")
    pg.add_argument("--known", type=int, default=50, help="Distinct known (member) hosts")
    pg.add_argument("--requests", type=int, default=500, help="Total requests")
    pg.add_argument("--miss-ratio", type=float, default=0.5, help="Fraction of unknown-host requests")
    pg.add_argument("--keep", action="store_true", help="Do not delete bench data after")
    pg.set_defaults(func=mode_gate)

    pr = sub.add_parser("rebuild", help="Prove the cache rebuilds on DB add/delete/status changes")
    pr.add_argument("--known", type=int, default=5, help="Bench domains to seed")
    pr.add_argument("--keep", action="store_true", help="Do not delete bench data after")
    pr.set_defaults(func=mode_rebuild)

    pcs = sub.add_parser("coldstart", help="Prove cold-start: fail-open (no outage) then gate-active after warm")
    pcs.add_argument("--known", type=int, default=5, help="Bench domains to seed")
    pcs.add_argument("--keep", action="store_true", help="Do not delete bench data after")
    pcs.set_defaults(func=mode_coldstart)

    pcm = sub.add_parser("compare", help="A/B DB-query load: cache OFF vs ON, count default queries")
    pcm.add_argument("--requests", type=int, default=2000, help="Requests replayed for each run")
    pcm.add_argument("--known", type=int, default=50, help="Distinct known (positive) hosts")
    pcm.add_argument("--miss-ratio", type=float, default=0.3, help="Fraction of miss requests (0..1)")
    pcm.add_argument("--unique-misses", action="store_true",
                     help="Each miss is a fresh host (cache-penetration/DoS shape)")
    pcm.add_argument("--keep", action="store_true", help="Do not delete bench data after")
    pcm.set_defaults(func=mode_compare)
    return p


def main():
    args = build_parser().parse_args()
    if getattr(args, "mode", None) == "load" and not args.duration and not args.requests:
        args.duration = 10.0        # sensible default
    args.func(args)


if __name__ == "__main__":
    main()
