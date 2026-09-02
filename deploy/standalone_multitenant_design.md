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

`settings.py`, evaluated at import time (it gates INSTALLED_APPS / DB backend /
middleware, so it cannot come from `settings_local` imported last, nor from the
DB):

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

## 3. Settings split — Phase 2 (refactored to a file)

`settings.py` is now the **standalone/shared base** (no scattered `if USE_MULTITENANT`).
All multi-tenant config lives in **`tenants_back/settings_multitenant.py`**, loaded at the
BOTTOM of `settings.py` — `if USE_MULTITENANT: from .settings_multitenant import *` —
BEFORE `settings_local` (so production overrides still win over both). The MT file imports
the base building blocks (`_DJANGO_APPS`/`_THIRD_PARTY_APPS`/`_BUSINESS_APPS`) and the
objects it augments (`DATABASES`/`CACHES`/`REST_FRAMEWORK`/`CELERY_BROKER_URL`), then
reassembles the union INSTALLED_APPS + overrides the rest. The mode flag resolves via
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
| `CACHES` beat / tenant_resolve | defined | omitted (only `default`) |
| `TENANT_RESOLVE` / `TENANT_REGISTRY` / `TENANT_BASE_DOMAINS` | defined | omitted |
| `CELERY_BEAT_SCHEDULER` | `TenantAwareDatabaseScheduler` | stock `DatabaseScheduler` (Celery redesign is separate) |

Shared (above the branch): `SECRET_KEY`, `AUTH_USER_MODEL`, DRF permissions,
`SIMPLE_JWT`, `CACHES["default"]`, i18n/static, TLS helpers, generic Celery.
`API_PATH_PREFIXES` and the beat-marker TTL constants are harmless plain values
and stay shared (no tenant reference).

## 3.1 Configuration tiers (which knob lives where, and why)

Config resolves in THREE tiers, in load order. The tier is dictated by WHEN a value is needed,
not by preference:

| Tier | Source (load order) | Read when | Use for | Examples |
|---|---|---|---|---|
| **bootstrap** | env var → `settings_mode.py` (gitignored) | settings-LOAD, before `django.conf.settings` exists | values consumed while `settings.py` runs — can't touch settings/DB yet | `USE_MULTITENANT`, `FANOUT_PERIOD_SECONDS` (both via `commons.platform.mode`) |
| **runtime** | `settings.py` / `settings_multitenant.py` (django settings) | request / task runtime (`django.conf.settings`) | everything read after boot | `TENANT_BEAT` knobs, `CELERY_BEAT_SCHEDULE`, queues, caches |
| **prod override** | `settings_local.py` (gitignored, loaded LAST) | settings-load, AFTER the two above | environment secrets/hosts that must win over base + MT overlay | DB creds, Redis URLs, bucket names |

Why the split is load-order, not taste:
- A **bootstrap** value CANNOT live in `settings_local.py` or `TENANT_BEAT` — both are read too
  late (the beat schedule is already baked at MT-overlay load; reading `django.conf.settings`
  mid-`settings.py` would cache an incomplete settings object). That is exactly why
  `FANOUT_PERIOD_SECONDS` is resolved by `commons.platform.mode.bootstrap_float()`
  (env → `settings_mode.py` → default), NOT a `TENANT_BEAT` key.
- `settings_local.py` loads LAST so production wins over both base and the MT overlay.
- **runtime** knobs (`TENANT_BEAT`) fall back to in-code `BEAT_DEFAULTS`; ship `TENANT_BEAT = {}`
  and override only what differs (no duplication of defaults).

## 4. `tenants` app boundary — Phase 2

Installed only in MT; absent in standalone: models (`Tenant/Shard/Domain/
ReservedHostRule`), `middleware`, `routers`, `context`, `resolver/*`,
`validators`, `admin` (`public_admin_site`), management commands, migrations,
`celery/*`. Audit confirms nothing outside `tenants` imports it (the one
exception, `users/admin.py`, was fixed via the facade).

## 5. `connection.schema_name` readers outside `tenants` — Phase 2

Audited (`grep -rn connection.schema_name` minus `tenants/`). Each must be
standalone-safe:

| File | Role | Standalone handling |
|---|---|---|
| `users/authentication.py` | `SchemaBoundJWTAuthentication` | **not wired** (DRF uses plain JWT) |
| `users/middleware.py` | `SchemaBoundSessionMiddleware` | **not wired** (not in MIDDLEWARE) |
| `users/serializers.py` | MT login serializers (schema claim) | **not reached** (stub uses stock `TokenObtainPairView`) |
| `users/signals.py` | `stamp_tenant_schema` (login) | **FIX**: defensive `getattr(connection,"schema_name",None)` → no-op in standalone |
| `users/permissions.py` | `_on_tenant()` — used by **all** business viewsets | **FIX**: mode-aware — returns `True` in standalone (no public/tenant split) |
| `products/management/commands/seed_products.py` | dev seed | **FIX**: mode-safe reads; the "skip public" guard is MT-only |

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
whatever CI it uses (decided at merge time). Three steps:

```sh
scripts/ci_guard_schema_name.sh    # §5 guard — connection.schema_name only in audited files
scripts/ci_mode.sh mt              # USE_MULTITENANT=1: check + makemigrations --check + full suite
scripts/ci_mode.sh standalone      # USE_MULTITENANT=0: check + makemigrations --check + business/users suite
```

As a matrix: run `ci_mode.sh` with `mode ∈ {mt, standalone}` in parallel, plus the
guard as its own job. (`PYTHON=…` overrides the interpreter.)

Notes:
- **DB-free.** The whole suite is `SimpleTestCase`, and `check` /
  `makemigrations --check` do not connect — so CI needs **no Postgres service**.
  Add a PostGIS service only when DB-backed tests are introduced.
- **No `settings_local.py` in CI.** It is gitignored and pins the django_tenants
  DB engine, which would mask the standalone backend; a clean checkout has none.
- **Standalone runs explicit labels** (`users` + business apps), never bare
  `manage.py test`: `tenants` is not installed, so importing `tenants/tests/*`
  (which import `tenants.models`) would fail at collection.
- The guard strips `#` comments before matching, so it flags real code reads, not
  prose. New un-audited reader → non-zero exit with the offending path.

The **YAML wrapper** (GitHub Actions / GitLab / …) is intentionally deferred to
the host-project merge — the scripts above are the whole substance.

## Phase order & status

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
