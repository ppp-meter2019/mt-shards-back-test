#!/usr/bin/env python3
"""Concurrent multi-tenant load test — stress RDS Proxy through the real API.

Per tenant we spawn N "virtual users" (VUs). Each VU keeps its OWN HTTP/1.1
connection (→ its own gunicorn worker → its own DB connection through RDS Proxy),
logs in once, then loops: create ONE OF EACH entity type (product, car, customer,
driver, order) → read each back → verify it persisted and is retrievable in this
tenant. Many VUs across all tenants at once exercises the proxy pool
(MaxConnectionsPercent), the borrow queue (ConnectionBorrowTimeout) and pinning.

Edit TENANTS + CONFIG below (no external file):

    pip install httpx
    python loadtest_rds_proxy.py
    python loadtest_rds_proxy.py --vus 100 --duration 120

Statistics are split per (entity, operation): CREATE vs READ, with a failure
taxonomy (HTTP codes, client exceptions, verify mismatches), sample failure
bodies, a per-tenant infra/proxy-signal breakdown, and a time bucketed error
timeline (to see WHEN the pool starts refusing under ramp).

Watch alongside on each proxy: CloudWatch DatabaseConnections /
DatabaseConnectionsCurrentlySessionPinned / DatabaseConnectionsBorrowLatency.

NOTE: the per-tenant login must be a staff/admin user allowed to create these
entities. `customer`/`driver` create a User → Django PBKDF2 hashing is CPU-heavy;
to isolate DB/proxy load from app CPU, trim ENABLED_ENTITIES to
{"product","car","order"}.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import httpx

# ===========================================================================
# CONFIG — edit inline. Fill in real usernames/passwords per tenant.
# ===========================================================================
TENANTS = [
    {"name": "alpha1", "base_url": "https://alpha1.test2-multitenants.isi-technology.com:8000",
     "username": "alpha_admin", "password": "Ms111111"},
    {"name": "beta",  "base_url": "https://beta.test2-multitenants.isi-technology.com:8000",
     "username": "REPLACE_ME", "password": "REPLACE_ME"},
    # add more tenants here...
]

VUS_PER_TENANT   = 50       # concurrent connections per tenant
DURATION_SECONDS = 30       # how long to sustain load
REQUEST_TIMEOUT  = 30.0     # per-request timeout (s) — a ReadTimeout here while workers
                            # wait on a 120s borrow IS the proxy-exhaustion signature
VERIFY_TLS       = False    # False for self-signed dev cert
BUCKET_SECS      = 5        # timeline granularity
FAIL_SAMPLE_CAP  = 5        # sample failure bodies to keep per (entity, op)

# One of each per iteration (order matters: `order` needs product+customer above it).
ENABLED_ENTITIES = ["product", "car", "customer", "driver", "order"]

_USER_PW = "LoadTest!9xQ"   # for auto-created customer/driver Users (passes validators)
_BRANDS = ["Volvo", "Scania", "MAN", "DAF", "Iveco", "Renault"]

# Errors that point at connection-pool / upstream exhaustion (i.e. RDS Proxy).
INFRA_STATUS = {502, 503, 504}
INFRA_EXC = {"ConnectTimeout", "ReadTimeout", "PoolTimeout", "WriteTimeout",
             "ConnectError", "RemoteProtocolError"}
# ===========================================================================


def _make_product(tag, ctx):
    return "/api/products/", {"name": f"lt-{tag}", "price": f"{random.randint(100, 99999) / 100:.2f}"}


def _make_car(tag, ctx):
    return "/api/cars/", {
        "brand": random.choice(_BRANDS), "model": f"M{random.randint(100, 999)}",
        "year": random.randint(2005, 2024), "license_plate": f"LT{uuid.uuid4().hex[:8]}",
    }


def _make_customer(tag, ctx):
    return "/api/customers/", {
        "username": f"cust-{tag}", "password": _USER_PW,
        "first_name": "Load", "last_name": "Test", "email": f"{tag}@lt.example",
        "phone": f"+1{random.randint(2000000000, 9999999999)}", "address": "1 Load Test Ave",
    }


def _make_driver(tag, ctx):
    return "/api/drivers/", {
        "username": f"drv-{tag}", "password": _USER_PW,
        "first_name": "Load", "last_name": "Driver",
        "date_of_birth": "1990-01-01", "license_number": f"DL{uuid.uuid4().hex[:8]}",
    }


def _make_order(tag, ctx):
    if "product" not in ctx:
        return None
    payload = {"items": [{"product": ctx["product"], "quantity": random.randint(1, 3)}]}
    if "customer" in ctx:
        payload["customer"] = ctx["customer"]
    return "/api/orders/", payload


ENTITY_BUILDERS = {
    "product": _make_product, "car": _make_car, "customer": _make_customer,
    "driver": _make_driver, "order": _make_order,
}


@dataclass
class OpStat:
    attempts: int = 0
    ok: int = 0
    verify_fail: int = 0                                # read: 200 but wrong/missing row
    status: Counter = field(default_factory=Counter)   # HTTP code -> n
    exc: Counter = field(default_factory=Counter)       # exception name -> n
    infra_500: int = 0                                  # HTTP 500 whose body looks like a DB/conn error
    latencies_ms: list = field(default_factory=list)
    samples: list = field(default_factory=list)         # [(code|exc, short body)]

    def http(self, code, ok, latency=None, body=""):
        self.attempts += 1
        self.status[code] += 1
        if ok:
            self.ok += 1
            if latency is not None:
                self.latencies_ms.append(latency)
        elif len(self.samples) < FAIL_SAMPLE_CAP:
            self.samples.append((code, " ".join(body.split())[:200]))

    def error(self, name, msg):
        self.attempts += 1
        self.exc[name] += 1
        if len(self.samples) < FAIL_SAMPLE_CAP:
            self.samples.append((name, " ".join(msg.split())[:200]))

    @property
    def fail(self):
        return self.attempts - self.ok

    @property
    def infra(self):
        return (sum(v for c, v in self.status.items() if c in INFRA_STATUS)
                + sum(v for n, v in self.exc.items() if n in INFRA_EXC)
                + self.infra_500)


class Metrics:
    """Single shared sink — safe because asyncio tasks are cooperative (no threads)."""

    def __init__(self):
        self.ops: dict[tuple, OpStat] = {}
        self.timeline = defaultdict(Counter)           # bucket -> Counter(ok/infra/other)
        self.start = time.monotonic()

    def op(self, tenant, entity, phase) -> OpStat:
        return self.ops.setdefault((tenant, entity, phase), OpStat())

    def mark(self, kind):
        self.timeline[int((time.monotonic() - self.start) // BUCKET_SECS)][kind] += 1


# Markers that mean a 500 is really a DB/connection failure surfacing (RDS Proxy
# borrow timeout / exhaustion → psycopg OperationalError → generic 500). Needs
# DEBUG=True or a body that leaks the message; otherwise 500s stay in "other".
_DB_ERR_MARKERS = (
    "operationalerror", "could not connect", "connection to server", "borrow timeout",
    "too many connections", "remaining connection slots", "server closed the connection",
    "connection already closed", "ssl error", "consuming input failed", "connection timed out",
)


def _is_db_error_body(body):
    b = body.lower()
    return any(m in b for m in _DB_ERR_MARKERS)


def classify(code, ok, body=""):
    if ok:
        return "ok"
    if code in INFRA_STATUS:
        return "infra"
    if code == 500 and _is_db_error_body(body):
        return "infra"
    return "other"


def pct(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    return values[max(0, min(len(values) - 1, int(round(q / 100 * (len(values) - 1)))))]


async def login(client, user, pw):
    r = await client.post("/api/auth/login/", json={"username": user, "password": pw})
    r.raise_for_status()
    return r.json()["access"]


async def create_and_verify(client, headers, tenant, vu_id, M) -> bool:
    """Create one of each ENABLED entity, then read each back. Returns saw_401."""
    ctx, created, saw_401 = {}, [], False
    for kind in ENABLED_ENTITIES:
        built = ENTITY_BUILDERS[kind](f"{tenant}-{vu_id}-{uuid.uuid4().hex[:8]}", ctx)
        if built is None:
            continue
        path, payload = built
        op = M.op(tenant, kind, "create")
        t0 = time.monotonic()
        try:
            cr = await client.post(path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            name = type(exc).__name__
            op.error(name, str(exc))
            M.mark("infra" if name in INFRA_EXC else "other")
            continue
        ok = cr.status_code in (200, 201)
        body = "" if ok else cr.text
        op.http(cr.status_code, ok, (time.monotonic() - t0) * 1000, body)
        kind = classify(cr.status_code, ok, body)
        if kind == "infra" and cr.status_code == 500:
            op.infra_500 += 1
        M.mark(kind)
        if cr.status_code == 401:
            saw_401 = True
        if not ok:
            continue
        oid = cr.json()["id"]
        ctx[kind] = oid
        created.append((kind, path, oid))

    for kind, path, oid in created:
        op = M.op(tenant, kind, "read")
        t1 = time.monotonic()
        try:
            vr = await client.get(f"{path}{oid}/", headers=headers)
        except httpx.HTTPError as exc:
            name = type(exc).__name__
            op.error(name, str(exc))
            M.mark("infra" if name in INFRA_EXC else "other")
            continue
        hit = vr.status_code == 200 and vr.json().get("id") == oid
        body = "" if hit else vr.text
        op.http(vr.status_code, hit, (time.monotonic() - t1) * 1000, body)
        if vr.status_code == 200 and not hit:
            op.verify_fail += 1
        kind = classify(vr.status_code, hit, body)
        if kind == "infra" and vr.status_code == 500:
            op.infra_500 += 1
        M.mark(kind)
    return saw_401


async def virtual_user(vu_id, tenant, deadline, M):
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    async with httpx.AsyncClient(base_url=tenant["base_url"], verify=VERIFY_TLS,
                                 timeout=httpx.Timeout(REQUEST_TIMEOUT),
                                 limits=limits, http2=False) as client:
        try:
            token = await login(client, tenant["username"], tenant["password"])
        except Exception as exc:  # noqa: BLE001
            M.op(tenant["name"], "-", "login").error(type(exc).__name__, str(exc))
            return
        headers = {"Authorization": f"Bearer {token}"}

        while time.monotonic() < deadline:
            try:
                if await create_and_verify(client, headers, tenant["name"], vu_id, M):
                    token = await login(client, tenant["username"], tenant["password"])
                    headers = {"Authorization": f"Bearer {token}"}
            except Exception as exc:  # noqa: BLE001
                M.op(tenant["name"], "-", "loop").error(type(exc).__name__, str(exc))
                await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- report
def _merge(stats):
    m = OpStat()
    for s in stats:
        m.attempts += s.attempts; m.ok += s.ok; m.verify_fail += s.verify_fail
        m.infra_500 += s.infra_500
        m.status.update(s.status); m.exc.update(s.exc)
        m.latencies_ms.extend(s.latencies_ms); m.samples.extend(s.samples)
    return m


def report(M: Metrics, wall):
    ops = M.ops
    tenants = [t["name"] for t in TENANTS]

    # 1) per (entity, operation), merged across tenants
    print("=" * 90)
    print(f"{'entity':<10}{'op':<8}{'attempts':>10}{'ok':>9}{'fail':>7}"
          f"{'verify_fail':>13}{'p95 ms':>9}   status")
    print("-" * 90)
    for kind in ENABLED_ENTITIES:
        for phase in ("create", "read"):
            merged = _merge([s for (t, e, p), s in ops.items() if e == kind and p == phase])
            if not merged.attempts:
                continue
            top = dict(merged.status.most_common(4))
            print(f"{kind:<10}{phase:<8}{merged.attempts:>10}{merged.ok:>9}{merged.fail:>7}"
                  f"{merged.verify_fail:>13}{pct(merged.latencies_ms, 95):>9.0f}   {top}")
    print("=" * 90)
    print(f"wall={wall:.1f}s  entities ok/s={sum(s.ok for s in ops.values()) / wall if wall else 0:.1f}")

    # 2) PROBLEMS split create / read, with sample bodies
    for phase in ("create", "read"):
        rows = [(e, s) for (t, e, p), s in ops.items() if p == phase and s.fail]
        if not rows:
            continue
        print(f"\nPROBLEMS ({phase})")
        seen = _merge_by_entity(rows)
        for entity, merged in sorted(seen.items()):
            reasons = Counter()
            for c, v in merged.status.items():
                if c >= 400:
                    label = f"HTTP {c}"
                    if c == 500 and merged.infra_500:
                        label = f"HTTP 500 ({merged.infra_500} DB/conn)"
                    reasons[label] += v
            for n, v in merged.exc.items():
                reasons[n] += v
            if not reasons:
                continue
            top_reasons = ", ".join(f"{r}×{n}" for r, n in reasons.most_common())
            print(f"  {entity:<10} {top_reasons}")
            for code, body in merged.samples[:3]:
                if body:
                    print(f"             e.g. [{code}] {body}")

    # 3) per-tenant infra/proxy-signal breakdown
    print("\nINFRA/PROXY SIGNAL per tenant "
          "(HTTP 502/503/504 + connect/read/pool timeouts + 500-with-DB-body)")
    for t in tenants:
        n = sum(s.infra for (tt, e, p), s in ops.items() if tt == t)
        attempts = sum(s.attempts for (tt, e, p), s in ops.items() if tt == t)
        rate = (100 * n / attempts) if attempts else 0
        flag = "  <-- pool likely saturated" if rate >= 1 else ""
        print(f"  {t:<16} {n:>7} signals / {attempts:>8} req  ({rate:.2f}%){flag}")

    # 4) error timeline (did failures start once the pool filled?)
    print(f"\nTIMELINE ({BUCKET_SECS}s buckets)   ok / infra / other")
    for b in sorted(M.timeline):
        c = M.timeline[b]
        print(f"  t={b * BUCKET_SECS:>3}-{b * BUCKET_SECS + BUCKET_SECS:<3}s   "
              f"{c['ok']:>7} / {c['infra']:>6} / {c['other']:>6}")

    vfail = sum(s.verify_fail for s in ops.values())
    if vfail:
        print(f"\n⚠️  {vfail} verify failures — created entity not read back in its own "
              f"tenant (isolation/routing problem, NOT a proxy issue).")


def _merge_by_entity(rows):
    out = {}
    for entity, s in rows:
        out.setdefault(entity, []).append(s)
    return {e: _merge(lst) for e, lst in out.items()}


async def run(vus, duration):
    if any(t["password"] == "REPLACE_ME" for t in TENANTS):
        raise SystemExit("Fill in real username/password in the TENANTS dict first.")
    M = Metrics()
    deadline = time.monotonic() + duration
    tasks = [virtual_user(vu, t, deadline, M) for t in TENANTS for vu in range(vus)]
    print(f"Starting {len(tasks)} VUs ({vus}/tenant × {len(TENANTS)} tenants), "
          f"{duration}s, entities/iter={ENABLED_ENTITIES}\n")
    wall0 = time.monotonic()
    await asyncio.gather(*tasks)
    report(M, time.monotonic() - wall0)


def main():
    ap = argparse.ArgumentParser(description="RDS Proxy multi-tenant load test")
    ap.add_argument("--vus", type=int, default=VUS_PER_TENANT, help="VUs per tenant")
    ap.add_argument("--duration", type=int, default=DURATION_SECONDS, help="seconds")
    args = ap.parse_args()
    asyncio.run(run(args.vus, args.duration))


if __name__ == "__main__":
    main()
