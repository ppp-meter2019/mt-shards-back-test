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

Run: `python scripts/ci_guard_ast.py <rule>` with rule in
{schema, redis, commons, settings_load, all}.
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
    # These compare against the public schema with the LITERAL "public": calling
    # get_public_schema_name() there would pull in django_tenants, NOT installed in
    # standalone. Deliberate — do not "fix" it.
    #
    # commons/platform/cache_keys.py used to be listed here too, for the single
    # `connections[alias].schema_name` read behind its `if settings.USE_MULTITENANT` branch.
    # That read now lives in tenants/context.py::current_schema_name — inside
    # SCHEMA_SKIP_ROOTS — so NOTHING under commons/ reads the routing axis any more, which is
    # what the facade's own docstring claims it is for. Keep it that way: a new read there
    # belongs in tenants.context, reached through commons.platform.tenancy.
    #
    # Anything added here must carry, at its comparison site, a note saying so.
    "users/serializers.py",
    "users/signals.py",
    "users/permissions.py",
    "products/management/commands/seed_products.py",
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


def _db_module_paths(tree: ast.AST) -> set[str]:
    """Dotted paths bound to the django.db MODULE itself, so `<path>.connection` counts too.

    `import django.db` binds "django.db"; `import django.db as d` binds "d"; `from django
    import db` binds "db". Without this the rule saw only the `from django.db import
    connection` spelling, and `db.connection.schema_name` walked straight through it —
    confirmed by running the guard against that exact file, which it passed.
    """
    paths = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "django.db":
                    paths.add(a.asname or "django.db")
        elif isinstance(node, ast.ImportFrom) and node.module == "django":
            for a in node.names:
                if a.name == "db":
                    paths.add(a.asname or a.name)
    return paths


def _dotted(node: ast.AST) -> str | None:
    """"a.b.c" for a Name/Attribute chain; None for anything else (a call, a subscript).

    Deliberately partial: it answers "is this expression a plain dotted path, and which one",
    which is all _db_module_paths needs. Anything more would be type inference.
    """
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def schema_offenders(tree: ast.AST) -> list[str]:
    """`<connection-ish>.schema_name` in any spelling the module's own imports allow.

    Known limit: a connection stashed on an attribute or passed as a parameter
    (`self.conn = connections[x]; self.conn.schema_name`) is not tracked — that would need
    type inference. It is a far narrower gap than the regex left, and `tenants/` (where such
    indirection actually happens) is skipped wholesale.
    """
    aliases = _connection_aliases(tree)
    db_paths = _db_module_paths(tree)
    if not aliases and not db_paths:
        return []

    def is_conn(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in aliases
        if isinstance(node, ast.Attribute) and node.attr in ("connection", "connections"):
            return _dotted(node.value) in db_paths             # db.connection, django.db.connections
        if isinstance(node, ast.Subscript):                    # connections[alias]
            return (isinstance(node.value, ast.Name) and node.value.id in aliases) \
                or is_conn(node.value)                         # db.connections[alias]
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

# django_redis is NOT the redis package, but one of its exports is a raw-client door — and in
# a django_redis project it is THE documented way in, so missing it left the rule's main hole.
# Narrowed to that one name on purpose: `from django_redis.util import default_reverse_key`
# hands out no client and must keep passing, which cache_keys.py's standalone branch relies on.
REDIS_WRAPPER_FACTORIES = {"get_redis_connection"}


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
            # The ORIGINAL name, never the asname: `import get_redis_connection as rc` used to
            # defeat this rule outright, since the Name branch below then sees only `rc`.
            bound = {a.name for a in node.names}
            if mod == "django_redis" or mod.startswith("django_redis."):
                if factories := bound & REDIS_WRAPPER_FACTORIES:
                    hits.append(f"from {mod} import {', '.join(sorted(factories))}")
            if (mod == "redis" or mod.startswith("redis.")) \
                    and mod not in REDIS_HARMLESS_SUBMODULES:
                if bound & REDIS_CLIENT_NAMES or mod != "redis":
                    hits.append(f"from {mod} import {', '.join(sorted(bound))}")
        elif isinstance(node, ast.Attribute) and node.attr in ("get_client", "master_client"):
            # ONLY the django_redis shape `<cache>.client.get_client(...)`. Matching the bare
            # attribute name flagged ANY object owning such a method, whatever the receiver —
            # a boto3 wrapper with `def get_client(self)` failed this rule and was told to use
            # django_redis_raw_client(), which is a different library entirely. Both sanctioned call
            # sites reach it through `.client`: caches["default"].client.get_client
            # (commons/platform/redis_client.py) and self.cache.client.get_client
            # (tenants/resolver/cache.py).
            #
            # The narrowing costs one spelling: `c = cache.client` on its own line, then
            # `c.get_client()`. Tracking that needs variable-binding inference, which is more
            # machinery than this guard is worth — and a two-line dodge is a deliberate act,
            # not the accident this rule exists to catch.
            if isinstance(node.value, ast.Attribute) and node.value.attr == "client":
                hits.append(f".client.{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in REDIS_CLIENT_NAMES:
            hits.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "get_redis_connection":
            hits.append("get_redis_connection")
    return hits


# ---------------------------------------------------------------------------
# rule: commons (app boundary)
# ---------------------------------------------------------------------------
# `commons/` must import cleanly in STANDALONE, where django_tenants is not installed at all.
# Section 4 of deploy/standalone_multitenant_design.md says so; until now only comments did.
# The facade reaches tenancy through `tenants.*` instead, inside an `if settings.USE_MULTITENANT`
# branch — that seam stays allowed, this rule is only about the upstream package.
#
# ANY depth, unlike the settings-load rule below: a deferred `import django_tenants` inside a
# function is not a fix here. The package is absent in standalone, so a lazy import merely
# moves the ImportError from boot to the first call — which is strictly worse.
def commons_django_tenants(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "django_tenants" or a.name.startswith("django_tenants."):
                    hits.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "django_tenants" or mod.startswith("django_tenants."):
                hits.append(f"from {mod} import ...")
    return hits


# ---------------------------------------------------------------------------
# rule: settings_load (D5 re-entrancy)
# ---------------------------------------------------------------------------
# These modules run MID-SETTINGS-LOAD: `tenants_back` is both the settings package and the
# Celery app package, so Django imports it on the way to `tenants_back.settings`. A module-level
# `django_tenants.utils` import there evaluates get_tenant_database_alias() in two default args
# at import time, which reads django.conf.settings and makes Django build a SECOND, incomplete
# Settings object. See tenants/context.py's get_public_schema_name docstring and
# test_settings_invariants.SettingsLoadReentrancyTests.
#
# Scope measured, not guessed: `import tenants_back` with settings unconfigured pulls in
# tenants_back{,.celery}, tenants{,.context}, tenants.celery.* and commons.platform.mode.
# commons/ is covered by the stricter `commons` rule above; re-measure with that import if the
# Celery bootstrap ever changes shape.
SETTINGS_LOAD_PATH = (
    "tenants/__init__.py",
    "tenants/context.py",
    "tenants/celery/",
    "tenants_back/__init__.py",
    "tenants_back/celery.py",
)


def _function_bodies(tree: ast.AST) -> set[int]:
    """id() of every node that sits inside a def — i.e. does NOT run at import."""
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                inside.add(id(child))
    return inside


def settings_load_django_tenants(tree: ast.AST) -> list[str]:
    """django_tenants imported at IMPORT TIME on the settings-load path.

    Deliberately allows the deferred form — an import inside a function body is the sanctioned
    fix (tenants/context.py does exactly that), so flagging it would forbid the remedy.
    """
    deferred = _function_bodies(tree)
    hits = []
    for node in ast.walk(tree):
        if id(node) in deferred:
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "django_tenants" or a.name.startswith("django_tenants."):
                    hits.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "django_tenants" or mod.startswith("django_tenants."):
                hits.append(f"from {mod} import ...")
    return hits


RULES = {
    "schema": dict(
        find=schema_offenders, allow=SCHEMA_ALLOW, skip_roots=SCHEMA_SKIP_ROOTS,
        ok="schema_name reads confined to audited files",
        fail="un-audited schema_name reader(s) outside tenants/",
        hint=("Make it standalone-safe (MT-gate on settings.USE_MULTITENANT or getattr with a\n"
              "safe default), then add it to SCHEMA_ALLOW in this file."),
    ),
    "commons": dict(
        find=commons_django_tenants, allow=set(), skip_roots=set(),
        only_prefixes=("commons/",),
        ok="commons/ imports no django_tenants (standalone-safe)",
        fail="django_tenants imported under commons/, which must load in standalone",
        hint=("commons/ is the mode facade: reach tenancy through commons.platform.tenancy\n"
              "(whose `tenants.*` imports sit inside `if settings.USE_MULTITENANT`), never\n"
              "through django_tenants directly. A deferred import is NOT a fix — the package\n"
              "is absent in standalone, so it only moves the ImportError to the first call."),
    ),
    "settings_load": dict(
        find=settings_load_django_tenants, allow=set(), skip_roots=set(),
        only_prefixes=SETTINGS_LOAD_PATH,
        ok="no import-time django_tenants on the settings-load path",
        fail="django_tenants imported at MODULE level on the settings-load path",
        hint=("Defer it: move the import INSIDE the function that needs it (see\n"
              "tenants/context.py::get_public_schema_name). A module-level import here runs\n"
              "mid-settings-load and makes Django build a second, incomplete Settings object."),
    ),
    "redis": dict(
        find=redis_offenders, allow=REDIS_ALLOW, skip_roots=set(),
        ok="raw redis-py access confined to the sanctioned choke points",
        fail="raw redis-py client acquired outside the two sanctioned choke points",
        hint=("Use commons.platform.redis_client.django_redis_raw_client(alias) for raw access\n"
              "(build them with cache_keys.tenant_key()), or ResolveCache.get_redis_raw_client()\n"
              "for the tenant-agnostic resolve cache. A THIRD choke point needs its key contract\n"
              "documented and an entry in REDIS_ALLOW in this file."),
    ),
}


def run(rule_name: str) -> int:
    rule = RULES[rule_name]
    checked = bad = 0
    only = rule.get("only_prefixes")
    for rel, path in iter_sources():
        if rel.parts[0] in rule["skip_roots"]:
            continue
        if only and not rel.as_posix().startswith(only):
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
