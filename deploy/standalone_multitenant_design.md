# Dual-mode: multi-tenant vs standalone (`USE_MULTITENANT`)

Status: Phase 1 done · Phase 2 in progress · Celery redesign deferred to a
separate step.

This project (`tenants_back`) is the **multi-tenant** platform (django-tenants
schema-per-tenant + a multi-shard router). It is also meant to be merged into a
larger existing project that is **not** multi-tenant (a single default DB, no
schemas). Rather than fork, the platform runs in **either** mode from one code
base, selected by a single boot-time flag `USE_MULTITENANT`.

- `USE_MULTITENANT = True`  → multi-tenant: `django_tenants` + shards, per-tenant
  schemas, host-based tenant resolution.
- `USE_MULTITENANT = False` → standalone: one `default` DB, no schemas, no tenant
  middleware/router — the host project's world.

**Standalone is the default.** The multi-tenant path is the opt-in layer on top.

---

## Locked decisions

1. **Default mode = standalone** (`USE_MULTITENANT` defaults to `False`).
2. Mode is resolved **without an env var and without editing `settings.py`**, by
   precedence: **env `USE_MULTITENANT=0/1` → `settings_mode.py` → default `False`**.
3. **PostGIS in both modes** (business models use GIS fields).
4. Standalone auth is **plain JWT** for now; bespoke auth needs surface at merge.
5. **Standalone URLs come from the host project's own `urls.py`.** Our repo keeps
   a *minimal* `urls_standalone.py` stub only so the app boots and CI can run.
6. The single flag-branch for app code lives in the **`commons/platform`** facade;
   business/core code never imports `tenants` directly.
7. The `routes` offline-S3 management commands are **MT-only and disposable** —
   they do not reach the merged project, so they are left untouched.

---

## 0. Invariants

1. Everything tenant-specific is isolated in the `tenants` app (MT-only).
2. Core/business code **never imports `tenants` directly** — only via
   `commons.platform`.
3. One `if USE_MULTITENANT` in the facade + **two branches in settings**. No flag
   checks scattered across the codebase — with the audited exceptions in §5.
4. **CI runs both modes.**

> **The real coupling marker is `connection.schema_name`, not the string
> `import tenants`.** `connection.schema_name` is injected by the
> `django_tenants` DB backend; on the plain PostGIS backend it does not exist
> (`AttributeError`). So "no `tenants` import" is necessary but **not sufficient**
> for standalone-safety — every runtime read of `connection.schema_name` outside
> the `tenants` app must be MT-gated or defensively read (§5).

---

## 1. Mode resolver — DONE (Phase 1)

`settings_base.py`, evaluated at import time (it gates INSTALLED_APPS / DB backend /
middleware, so it cannot come from a local settings file imported last, nor from the
DB). The dispatcher `settings.py` calls the same resolver to pick its branch:

```python
if "USE_MULTITENANT" in os.environ:
    USE_MULTITENANT = os.environ["USE_MULTITENANT"] == "1"
else:
    try:
        from .settings_mode import USE_MULTITENANT
    except ImportError:
        USE_MULTITENANT = False          # default: standalone
```

- `settings_mode.py` is gitignored; `settings_mode.py.example` documents it.
- **This dev repo** ships a local `settings_mode.py` with `USE_MULTITENANT = True`
  so day-to-day work and the test suite stay multi-tenant. CI overrides per job
  with the env var.

## 2. Facade `commons/platform/` — DONE (Phase 1)

- `tenancy.py` — `schema_context` / `tenant_context` / `use_alias` /
  `get_public_schema_name`: real (MT) vs no-op (standalone).
- `admin.py` — `management_site()`: the public-host admin site (MT) vs `None`.
- `commands.py` — `TenantCommand(BaseCommand)`: refuses to run when
  `USE_MULTITENANT` is off.
- `users/admin.py` rewired onto `management_site()` — the only real cross-app
  `tenants` import outside the app, now removed.

## 3. Settings split — Phase 2 (three files + a dispatcher)

| File | Role |
|---|---|
| `settings_base.py` | the **standalone/shared base** — complete on its own, no scattered `if USE_MULTITENANT` |
| `settings_multitenant.py` | the MT overlay; **star-imports the base itself**, then augments it — except `DATABASES`, which it REPLACES |
| `settings.py` | the **dispatcher**: picks one branch, then applies that mode's local file |
| `settings_local.py` / `settings_local_multitenant.py` | production overrides, **one per mode**, applied LAST so they win over base AND overlay |

### 3.0 One local settings file PER MODE

One local settings file shared by both modes could not work. A deployed multi-tenant one
pins `ENGINE` to the django-tenants backend and declares the `tenant_*` shards, so loading it
under `USE_MULTITENANT=0` produced a "standalone" config running on the multi-tenant backend.
`scripts/ci_mode.sh` used to work around exactly that, by REQUIRING the file to be absent in
CI. Two names make the separation structural.

Standalone keeps the PLAIN name `settings_local.py`, because standalone is the host project's
mode — the MT layer drops into a project that already has one, and nothing on that side needs
renaming. Only the multi-tenant file carries a suffix: `settings_local_multitenant.py`.

The load stays in the dispatcher rather than moving to the bottom of `settings_base.py` /
`settings_multitenant.py`, for two reasons:

1. A local file does `from .settings import DATABASES, …`, which resolves only because by
   that point `tenants_back.settings` already has those names bound by its star-import.
   Loaded from inside `settings_multitenant.py`, `tenants_back.settings` would still be a
   **partially initialized** module holding nothing — `ImportError`. Same class of breakage
   as the pre-split layout described below.
2. `settings_base.py` would need an `if USE_MULTITENANT` at its bottom, or the overlay's
   `from .settings_base import *` would drag the STANDALONE production overrides into MT.
   Not having that `if` in the base is the whole point of the split.

Both `except` clauses in the dispatcher are narrowed to "this exact module does not exist".
An `ImportError` raised INSIDE a local file now propagates instead of being swallowed into a
silent boot on dev defaults.

### 3.0b Multi-tenant builds its own `DATABASES`

`settings_multitenant.py` does **not** derive `DATABASES` from the base. The base `default`
is the STANDALONE database — in the host project, its real single-tenant production DB — so
the old `{**DATABASES, "default": {**DATABASES["default"], …}}` meant that flipping
`USE_MULTITENANT` on aimed django-tenants at whatever database the base happened to name.
The overlay now rebinds `DATABASES` to a literal dict: a localhost dev `default` on its own
`NAME`, and nothing else. Every real cluster and every `tenant_*` shard is declared in
`settings_local_multitenant.py`, which also makes the local file the sole source of the shard
universe (`Shard.clean()` refuses an alias absent from `DATABASES`). Pinned by
`test_settings_invariants.MultitenantDatabasesContractTests`.

`DJANGO_SETTINGS_MODULE` stays `tenants_back.settings` everywhere (manage.py, wsgi, asgi,
celery, `bin/gunicorn_start.sh`, `scripts/`) — the mode is chosen inside the dispatcher, not
by pointing Django at a different module, so a deployment switches modes with an env var and
no config edits.

The dependency runs **base <- overlay**, one way. An earlier layout had `settings.py` BE the
base with the overlay importing names back out of it; that worked only while every imported
name happened to be defined above the overlay's import line, and reordering the base broke
boot with `cannot import name X from partially initialized module`. The overlay now gets the
base blocks (`_DJANGO_APPS`/`_THIRD_PARTY_APPS`/`_BUSINESS_APPS`) and the objects it augments
(`DATABASES`/`CACHES`/`REST_FRAMEWORK`/`CELERY_BROKER_URL`) from a plain forward
`from .settings_base import *`, then reassembles the union INSTALLED_APPS + overrides the rest.

> **Trap.** `import *` skips underscore-prefixed names, so the dispatcher must RE-EXPORT
> `_aurora_db_options` / `_proxy_db_options` explicitly — a deployed
> `settings_local_multitenant.py` imports them from `.settings`. Both helpers (and
> `AWS_RDS_CA`) live in `settings_multitenant.py`, not in the base: Aurora + RDS Proxy is
> multi-tenant deployment topology, and the standalone branch deliberately does NOT re-export
> them. Without the re-export line the local file raises ImportError at boot. Pinned by
> `test_settings_invariants.SettingsLocalContractTests`. The mode flag resolves via
`commons/platform/mode.py::use_multitenant()` (settings-load-safe — does NOT read
`django.conf.settings`, which would cache incomplete settings). Verified behavior-preserving:
resolved settings are byte-identical before/after the split in both modes.

The base vs MT mapping (each was previously an inline `if USE_MULTITENANT: … else: …`):

| Setting | Multi-tenant | Standalone |
|---|---|---|
| `INSTALLED_APPS` | `SHARED_APPS + TENANT_APPS` incl. `tenants`, `django_tenants` | plain list, **no** `tenants`/`django_tenants` |
| DB `ENGINE` | `django_tenants.postgresql_backend` (+ `ORIGINAL_BACKEND` postgis) | `django.contrib.gis.db.backends.postgis` |
| `DATABASE_ROUTERS` | tenant router + `TenantSyncRouter` | `[]` |
| `TENANT_MODEL` / `*_URLCONF` | set | unset |
| `ROOT_URLCONF` | `urls_tenant` (+ `PUBLIC_SCHEMA_URLCONF`) | `tenants_back.urls_standalone` (stub; host project overrides) |
| `MIDDLEWARE` | + 2 tenant middlewares + diagnostics + `SchemaBoundSessionMiddleware` | base only, stock `SessionMiddleware` |
| DRF auth | `SchemaBoundJWTAuthentication` | `rest_framework_simplejwt…JWTAuthentication` |
| `CACHES` `tenant_resolve` / `beat_lock` | defined | omitted (only `default`) |
| `TENANT_RESOLVE` / `TENANT_REGISTRY` / `TENANT_BASE_DOMAINS` | defined | omitted |
| `CELERY_BEAT_SCHEDULER` | `redbeat.RedBeatScheduler` (+ `CELERY_REDBEAT_*`) | unset — Celery's default `PersistentScheduler` (or the host project's own) |

Shared (above the branch): `SECRET_KEY`, `AUTH_USER_MODEL`, DRF permissions,
`SIMPLE_JWT`, `CACHES["default"]`, i18n/static, TLS helpers, generic Celery.
`API_PATH_PREFIXES` is a harmless plain value and stays shared (no tenant reference).

## 3.1 Configuration tiers (which knob lives where, and why)

Config resolves in THREE tiers, in load order. The tier is dictated by WHEN a value is needed,
not by preference:

| Tier | Source (load order) | Read when | Use for | Examples |
|---|---|---|---|---|
| **bootstrap** | env var → `settings_mode.py` (gitignored) | settings-LOAD, before `django.conf.settings` exists | values consumed while `settings.py` runs — can't touch settings/DB yet | `USE_MULTITENANT`, `FANOUT_PERIOD_SECONDS` (both via `commons.platform.mode`) |
| **runtime** | `settings.py` / `settings_multitenant.py` (django settings) | request / task runtime (`django.conf.settings`) | everything read after boot | `TENANT_BEAT` knobs, `CELERY_BEAT_SCHEDULE`, queues, caches |
| **prod override** | `settings_local_<mode>.py` (gitignored, loaded LAST) | settings-load, AFTER the two above | environment secrets/hosts that must win over base + MT overlay | DB creds, Redis URLs, bucket names |

Why the split is load-order, not taste:
- A **bootstrap** value CANNOT live in a local settings file or `TENANT_BEAT` — both are read too
  late (the beat schedule is already baked at MT-overlay load; reading `django.conf.settings`
  mid-`settings.py` would cache an incomplete settings object). That is exactly why
  `FANOUT_PERIOD_SECONDS` is resolved by `commons.platform.mode.bootstrap_float()`
  (env → `settings_mode.py` → default), NOT a `TENANT_BEAT` key.
- The mode's local file loads LAST so production wins over both base and the MT overlay.
- **runtime** knobs (`TENANT_BEAT`) fall back to in-code `BEAT_DEFAULTS`; ship `TENANT_BEAT = {}`
  and override only what differs (no duplication of defaults).

## 4. `tenants` app boundary — Phase 2

Installed only in MT; absent in standalone: models (`Tenant/Shard/Domain/
ReservedHostRule`), `middleware`, `routers`, `context`, `resolver/*`,
`validators`, `admin` (`public_admin_site`), management commands, migrations,
`celery/*`.

Invariant 2 says business code reaches `tenants` only through `commons.platform`. What
follows is the KNOWN STATE, not the result of a one-off audit — this paragraph used to say
"audit confirms nothing outside `tenants` imports it", which was true when written and then
went stale in silence:

| Importer | Status |
|---|---|
| `users/admin.py` | **fixed** — goes through `commons.platform.admin.management_site()` |
| `routes/management/commands/{list,loadtest}_offline_coordinates_s3.py` | **open, accepted** — `from tenants.models import Tenant`. Loadtest/ops tooling that does not migrate to the host project, so a facade entry is not worth it. Running either under `USE_MULTITENANT=0` fails at import with `AttributeError: 'Settings' object has no attribute 'TENANT_MODEL'` — an unhelpful message, but out of reach of anything that ships. |

**No guard enforces this invariant.** The five static checks cover `connection.schema_name`,
the routing axis, context imports, the console boundary and raw redis-py — not this one. A
sixth rule in `scripts/ci_guard_ast.py` would be a few lines (an `ast.ImportFrom` whose module
starts with `tenants.`, outside `tenants/`), and would need the two commands above in its
allowlist.

## 5. `connection.schema_name` readers outside `tenants` — Phase 2

Audited. The authoritative list is `SCHEMA_ALLOW` in `scripts/ci_guard_ast.py`; the table
below mirrors it with the reasoning. It is no longer reproducible by grepping for
`connection.schema_name`: the guard matches the ATTRIBUTE on any connection-bound name, so
`conn.schema_name` after `from django.db import connection as conn` counts too — a form the
old grep silently missed. Each entry must be standalone-safe:

| File | Role | Standalone handling |
|---|---|---|
| `users/authentication.py` | `SchemaBoundJWTAuthentication` | **not wired** (DRF uses plain JWT) |
| `users/middleware.py` | `SchemaBoundSessionMiddleware` | **not wired** (not in MIDDLEWARE) |
| `users/serializers.py` | MT login serializers (schema claim) | **not reached** (stub uses stock `TokenObtainPairView`) |
| `users/signals.py` | `stamp_tenant_schema` (login) | **FIX**: defensive `getattr(connection,"schema_name",None)` → no-op in standalone |
| `users/permissions.py` | `_on_tenant()` — used by **all** business viewsets | **FIX**: mode-aware — returns `True` in standalone (no public/tenant split) |
| `products/management/commands/seed_products.py` | dev seed | **FIX**: mode-safe reads; the "skip public" guard is MT-only |
| `commons/platform/cache_keys.py` | Redis key token (`tenant:<schema>:…`) | **BRANCH**: the read sits inside `if settings.USE_MULTITENANT`, so it cannot execute on the plain PostGIS backend. Stronger than the rows above — in standalone the module is never imported at all, since no `KEY_FUNCTION` is wired there |

`users/permissions.py` is the critical one: every business viewset
(`cars/drivers/products/orders`) gates on `_on_tenant()`. Left unchanged,
standalone would either 500 (`AttributeError`) or deny every request (no schema
is ever `!= "public"`).

## 6. Standalone `urls_standalone.py` stub — Phase 2 (this repo only)

Minimal, whose only job is to let the app boot and CI run:

- `api/health/` — inline `JsonResponse` (does **not** import `tenants.views`);
- `admin/` — default `admin.site`;
- `api/auth/login/` + `refresh/` — stock SimpleJWT views (no schema claim);
- the business router (cars/drivers/customers/products/orders/routes) so
  standalone CI can smoke business endpoints against the single DB.

At integration the host project's `urls.py` becomes `ROOT_URLCONF`; this stub is
**not** merged (like the `routes` offline commands).

## 7. Auth / middleware — Phase 2

MT: `SchemaBoundJWTAuthentication` + `SchemaBoundSessionMiddleware`. Standalone:
stock `JWTAuthentication` + stock `SessionMiddleware`. The `users`
authentication/middleware modules do not import `tenants`, so they are installable
in both modes but simply not wired in standalone (§5).

## 8. Backend / migrations — Phase 2

Business migrations run in **both** modes (standalone → `default`; MT → schemas).
`tenants` migrations are MT-only. Standalone uses plain `migrate`; MT uses
`migrate_schemas` (our override forbids plain `migrate`).

## 9. Management commands — Phase 4 (DONE)

All 10 `tenants/management/commands/*` retrofitted onto
`commons.platform.commands.TenantCommand` (whose `execute()` refuses with a clear
`CommandError` when `USE_MULTITENANT` is off — belt-and-suspenders, since the
`tenants` app is also physically absent in standalone). `migrate_schemas` keeps its
django_tenants base via `class Command(TenantCommand, UpstreamCommand)` — the guard
sits first in the MRO and runs before the upstream flow (we override `handle()`, not
`execute()`, so it is never bypassed).

- `products/seed_products` stays a plain `BaseCommand` — it is a business command
  that runs in BOTH modes (already made mode-safe in §5).
- `routes/*offline*_coordinates_s3` are intentionally left as plain `BaseCommand`:
  they are MT-only but disposable (removed before the merge), and `routes` IS
  installed in standalone. If they must survive the merge, guard them too.

Coverage is locked by `tenants/tests/test_commands.py::TenantCommandGuardTests`:
every name in `TENANT_COMMANDS` must be a `TenantCommand` and must refuse under
`@override_settings(USE_MULTITENANT=False)`.

## 10. Admin — partly Phase 1

Business models register on the default `admin.site` in both modes.
`public_admin_site` and the tenant-management admin are MT-only. `User`
registration goes through `management_site()` (done).

## 11. CI — Phase 3

The CI logic is platform-agnostic shell, so the host project can wire it into
whatever CI it uses (decided at merge time). Two mode jobs plus the static guards:

```sh
scripts/ci_mode.sh mt              # USE_MULTITENANT=1: check + makemigrations --check + full suite
scripts/ci_mode.sh standalone      # USE_MULTITENANT=0: check + makemigrations --check + business/users suite

for g in scripts/ci_guard_*.sh; do "$g"; done   # all static guards; each is DB-free
```

Two of the five (`ci_guard_schema_name.sh`, `ci_guard_redis_client.sh`) are now thin wrappers
over `scripts/ci_guard_ast.py`, which parses SYNTAX instead of matching text. They used to be
greps, and greps failed them three ways: they enumerated spellings (so `redis.from_url()` —
one of the two documented ways to build a client — went unseen), they anchored on the literal
name `connection` (so one `import connection as conn` disabled the schema rule entirely), and
they treated source as text (so a file that merely MENTIONED a pattern in a docstring failed,
which in settings modules that are over half prose was a matter of time). The allowlists live
in that file. The other three are still greps and carry the same limitations.

**Nothing runs the guards today.** There is no `.github/`, `Makefile`, `tox.ini`, pre-commit
config or git hook in this repo, and `ci_mode.sh` does not invoke them — so until the host
project wires the loop above, each guard is a check someone has to type. Treat the table
below as "what this check REPORTS when run", not as an enforced invariant.

| Guard | Reports |
|---|---|
| `ci_guard_schema_name.sh` | §5 — an un-audited read of the connection's `schema_name` outside `tenants/`, in ANY spelling the module's own imports allow, which breaks standalone (the attribute does not exist on the plain PostGIS backend). Model fields that share the name (`Tenant.schema_name`) are correctly ignored |
| `ci_guard_context_import.sh` | context helpers imported from `django_tenants.utils` instead of `tenants.context` (the `apps.ready()` monkeypatch is partial by nature — `deploy/UPSTREAM_FORK.md` §4) |
| `ci_guard_routing_axis.sh` | the routing axis escaping `tenants/context.py` |
| `ci_guard_console_boundary.sh` | runtime code importing `tenants.console` (one-way boundary) |
| `ci_guard_redis_client.sh` | anything that can yield a raw redis-py client outside the two sanctioned choke points, where `KEY_FUNCTION` cannot tenant-scope the keys (`deploy/redis_keys_design.md` §4). `from redis.exceptions import …` is correctly ignored — handling a Redis error is not acquiring a client |

As a matrix: run `ci_mode.sh` with `mode ∈ {mt, standalone}` in parallel, plus the
guards as their own job. (`PYTHON=…` overrides the interpreter.)

Notes:
- **DB-free.** The whole suite is `SimpleTestCase`, and `check` /
  `makemigrations --check` do not connect — so CI needs **no Postgres service**.
  Add a PostGIS service only when DB-backed tests are introduced.
- **No local settings file in CI.** Both are gitignored and a clean checkout has
  neither. Since the split into `settings_local.py` /
  `settings_local_multitenant.py`, a stray multi-tenant one can no longer leak its
  django_tenants DB engine into the standalone run.
- **Standalone runs explicit labels** (`users` + business apps), never bare
  `manage.py test`: `tenants` is not installed, so importing `tenants/tests/*`
  (which import `tenants.models`) would fail at collection.
- The guard strips `#` comments before matching, so it flags real code reads, not
  prose. New un-audited reader → non-zero exit with the offending path.

The **YAML wrapper** (GitHub Actions / GitLab / …) is intentionally deferred to
the host-project merge — the scripts above are the whole substance.

## Phase order & status

> Test counts in this list are END-OF-PHASE snapshots, not the current state. The suite
> only grows, so a number here dates its entry rather than describing the tree; for the
> live figure run `scripts/ci_mode.sh mt`.

1. **Phase 1 — DONE**: mode resolver (`settings_mode.py`) + `commons/platform` facade
   + `users/admin.py` rewire. (132 tests green, `check` clean, both facade
   branches verified.)
2. **Phase 2 — in progress**: two-branch settings (§3,4,7,8) + `urls_standalone.py`
   stub (§6) + the §5 fixes + flip the default to `False` (+ ship a local
   `settings_mode.py=True` for this dev repo).
3. **Phase 3 — DONE** (scripts): `scripts/ci_guard_schema_name.sh` +
   `scripts/ci_mode.sh {mt,standalone}` (§11). Verified locally: guard OK, MT 147
   tests, standalone 11 tests (incl. `users.tests.OnTenantModeTests` +
   `StandaloneUrlconfTests`, which run under the real USE_MULTITENANT=0). The CI
   YAML wrapper is deferred to the host-project merge (platform TBD there).
4. **Phase 4 — DONE**: all 10 tenant commands on `TenantCommand` (§9), guarded +
   covered by `TenantCommandGuardTests`. MT suite now 140 tests.
5. **Deferred (separate step)**: Celery/beat `USE_MULTITENANT` gate + fanout
   redesign.
