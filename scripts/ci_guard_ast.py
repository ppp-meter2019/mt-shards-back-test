#!/usr/bin/env python
"""Static guards that parse SYNTAX, not text.

Both rules here used to be grep patterns. Regexes failed them in three ways, all confirmed by
running them:

  * they enumerated SPELLINGS of a call, so anything unanticipated slipped past. The redis
    rule caught `redis.Redis(` and missed `redis.from_url(` and `Redis.from_url(` — the two
    documented ways to build a client from a URL. Six of ten real spellings were missed.
  * they anchored on the literal name `connection`, so one `from django.db import connection
    as conn` disabled the schema rule completely; `connections[ALIASES[0]]` also escaped,
    because `[^]]*` cannot span a nested bracket.
  * they treated source as text. Comment stripping was `${code%%#*}`, which handles `#` and
    nothing else — so a file that merely MENTIONED a pattern in a docstring failed CI. In a
    codebase whose settings modules are over half prose, that collision was a matter of time,
    and it punished exactly the documentation this project relies on.

`ast` has none of those failure modes: it sees attribute access and imports regardless of how
the object was named, and it never sees a string literal as code.

Run: `python scripts/ci_guard_ast.py <rule>` with rule in {schema, redis, all}.
Nothing invokes this automatically — scripts/ci_mode.sh runs `check`, `makemigrations --check`
and the test suite, and does not touch the guards.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"p_env", "__pycache__", "migrations", ".git", "node_modules"}


def iter_sources():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if SKIP_DIRS & set(rel.parts):
            continue
        yield rel, path


# ---------------------------------------------------------------------------
# rule: schema
# ---------------------------------------------------------------------------
# `connection.schema_name` is the REAL multi-tenant coupling marker: the attribute is injected
# by the django_tenants DB backend, and on the plain PostGIS backend used in standalone it does
# NOT exist -> AttributeError. A "no `import tenants`" audit is necessary but not sufficient.
#
# Matching the ATTRIBUTE rather than the object is the whole point: it does not matter whether
# the connection is reached as `connection`, `conn`, `connections[alias]`,
# `connections[ALIASES[0]]` or `self.connection`.
SCHEMA_ALLOW = {
    # Audited, standalone-safe readers (each MT-gated or getattr-guarded / not wired).
    # Two standalone-safe ways to compare against the public schema, and both are in use:
    #   a) the literal "public" — the users/* and products/* readers. Calling
    #      get_public_schema_name() there would pull in django_tenants, NOT installed in
    #      standalone. Deliberate — do not "fix" it.
    #   b) commons.platform.tenancy.get_public_schema_name — the mode facade, which returns
    #      the literal in standalone. Used by commons/platform/cache_keys.py, whose read is
    #      itself inside an `if settings.USE_MULTITENANT` branch.
    # Anything added here must carry, at its comparison site, a note saying which it uses.
    "users/authentication.py",
    "users/middleware.py",
    "users/serializers.py",
    "users/signals.py",
    "users/permissions.py",
    "products/management/commands/seed_products.py",
    "commons/platform/cache_keys.py",
}
SCHEMA_SKIP_ROOTS = {"tenants"}   # the app that owns the axis


def _connection_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to django.db.connection / connections in THIS module.

    This is what a regex could never do, and it is why aliasing no longer defeats the rule:
    `from django.db import connection as conn` binds "conn" here, so `conn.schema_name` is
    matched exactly like `connection.schema_name`.

    It also keeps the rule OFF `Tenant.schema_name`, which is a model FIELD that merely shares
    the name — matching the bare attribute flagged every command that prints a tenant, which
    would have made the allowlist meaningless.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "django.db":
            for a in node.names:
                if a.name in ("connection", "connections"):
                    names.add(a.asname or a.name)
    return names


def schema_offenders(tree: ast.AST) -> list[str]:
    """`<connection-ish>.schema_name` in any spelling the module's own imports allow.

    Known limit: a connection stashed on an attribute or passed as a parameter
    (`self.conn = connections[x]; self.conn.schema_name`) is not tracked — that would need
    type inference. It is a far narrower gap than the regex left, and `tenants/` (where such
    indirection actually happens) is skipped wholesale.
    """
    aliases = _connection_aliases(tree)
    if not aliases:
        return []

    def is_conn(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in aliases
        if isinstance(node, ast.Subscript):                    # connections[alias]
            return isinstance(node.value, ast.Name) and node.value.id in aliases
        return False

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "schema_name" and is_conn(node.value):
            hits.append("<connection>.schema_name")
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and len(node.args) >= 2
              and is_conn(node.args[0])
              and isinstance(node.args[1], ast.Constant)
              and node.args[1].value == "schema_name"):
            hits.append('getattr(<connection>, "schema_name")')
    return hits


# ---------------------------------------------------------------------------
# rule: redis
# ---------------------------------------------------------------------------
# KEY_FUNCTION tenant-scopes only the keys that pass through the Django cache API. It does
# nothing for a raw redis-py client, and the structures that most need scoping are exactly the
# raw ones (the four aggregate hashes of RGKB section 28, plus the VTL keys). So the discipline
# is a short allowlist plus this check.
#
# `.get_client` is the important one: not an obvious `import redis`, but a bypass THROUGH
# django_redis that reads like ordinary cache work on review.
REDIS_ALLOW = {
    "tenants/resolver/cache.py",          # CACHES['tenant_resolve'], tenant-AGNOSTIC
    "commons/platform/redis_client.py",   # CACHES['default'], keys via cache_keys.tenant_key()
}


# Submodules that cannot hand out a client. Importing an exception type to handle a Redis
# failure is not "acquiring a client", and five first-party modules legitimately do it.
REDIS_HARMLESS_SUBMODULES = {"redis.exceptions", "redis.typing"}
# Names that DO hand one out, however they are reached.
REDIS_CLIENT_NAMES = {"Redis", "StrictRedis", "ConnectionPool", "BlockingConnectionPool",
                      "from_url", "get_redis_connection"}


def redis_offenders(tree: ast.AST) -> list[str]:
    """Anything that can yield a redis-py client, whatever the spelling.

    The regex this replaced enumerated constructors and so missed `redis.from_url()` and
    `Redis.from_url()` — the two documented ways to build one from a URL — along with
    `ConnectionPool`, `Redis(...)` bound by a submodule import, and every `redis.<sub>` import.
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if (a.name == "redis" or a.name.startswith("redis.")) \
                        and a.name not in REDIS_HARMLESS_SUBMODULES:
                    hits.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if (mod == "redis" or mod.startswith("redis.")) \
                    and mod not in REDIS_HARMLESS_SUBMODULES:
                bound = {a.name for a in node.names}
                if bound & REDIS_CLIENT_NAMES or mod != "redis":
                    hits.append(f"from {mod} import {', '.join(sorted(bound))}")
        elif isinstance(node, ast.Attribute) and node.attr in (
                "get_client", "master_client", *REDIS_CLIENT_NAMES):
            hits.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "get_redis_connection":
            hits.append("get_redis_connection")
    return hits


RULES = {
    "schema": dict(
        find=schema_offenders, allow=SCHEMA_ALLOW, skip_roots=SCHEMA_SKIP_ROOTS,
        ok="schema_name reads confined to audited files",
        fail="un-audited schema_name reader(s) outside tenants/",
        hint=("Make it standalone-safe (MT-gate on settings.USE_MULTITENANT or getattr with a\n"
              "safe default), then add it to SCHEMA_ALLOW in this file."),
    ),
    "redis": dict(
        find=redis_offenders, allow=REDIS_ALLOW, skip_roots=set(),
        ok="raw redis-py access confined to the sanctioned choke points",
        fail="raw redis-py client acquired outside the two sanctioned choke points",
        hint=("Use commons.platform.redis_client.tenant_raw_client() for tenant-scoped keys\n"
              "(build them with cache_keys.tenant_key()), or ResolveCache.get_redis_raw_client()\n"
              "for the tenant-agnostic resolve cache. A THIRD choke point needs its key contract\n"
              "documented and an entry in REDIS_ALLOW in this file."),
    ),
}


def run(rule_name: str) -> int:
    rule = RULES[rule_name]
    checked = bad = 0
    for rel, path in iter_sources():
        if rel.parts[0] in rule["skip_roots"]:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        except SyntaxError as exc:
            print(f"FAIL: cannot parse {rel}: {exc}", file=sys.stderr)
            return 1
        hits = rule["find"](tree)
        if not hits:
            continue
        checked += 1
        if str(rel) in rule["allow"]:
            continue
        if bad == 0:
            print(f"FAIL: {rule['fail']}:", file=sys.stderr)
        bad += 1
        print(f"  - {rel}  ({', '.join(sorted(set(hits)))})", file=sys.stderr)

    if bad:
        print(rule["hint"], file=sys.stderr)
        return 1
    print(f"OK: {rule['ok']} ({checked} file(s) with a match).")
    return 0


if __name__ == "__main__":
    names = sys.argv[1:] or ["all"]
    if names == ["all"]:
        names = list(RULES)
    unknown = [n for n in names if n not in RULES]
    if unknown:
        sys.exit(f"unknown rule(s): {', '.join(unknown)}; expected {', '.join(RULES)} or all")
    sys.exit(max(run(n) for n in names))
