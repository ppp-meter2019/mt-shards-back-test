# Database setup & initialization — under the hood

The README's **Initialization from scratch** table tells you *what to run* and
*what you should see*. This document is the companion deep-dive: it explains
**what each step actually does under the hood** — cluster topology, per-cluster
preparation, credentials, TLS, the Django settings wiring, and the exact
migration sequence that turns three empty Aurora clusters into a working
multi-tenant platform. It is the source of truth for anything schema- or
connection-related; the README covers the broader infra runbook (ALB, EC2,
nginx, supervisor).

---

## 1. Topology

Three Aurora PostgreSQL clusters, one Django database alias each:

| Alias | Cluster | Hosts | Shard role |
|---|---|---|---|
| `default`  | `tenants-default`  | the `public` schema only: `Shard`, `Tenant`, `Domain`, platform `User`s, sessions | `is_default=True` |
| `tenant_1` | `tenants-tenant-1` | business tenant schemas (`acme`, `beta`, …) | `is_default=False` |
| `tenant_2` | `tenants-tenant-2` | business tenant schemas (`globex`, …) | `is_default=False` |

Invariants enforced in code (do not rely on operator discipline):

- The `default` database **never** holds a business-tenant schema — guarded by
  `Tenant.clean()`, `ShardAdminForm`, the router's `allow_migrate`, and every
  management command.
- Each business tenant is pinned to exactly one shard via `Tenant.shard`
  (FK, `on_delete=PROTECT`).
- A tenant's ORM queries are routed to `Tenant.shard.alias` by
  `TenantDatabaseRouter` reading the `current_db` ContextVar.

Every cluster runs the **same** application database name (`tenants_back`) and
the **same** schema layout mechanism; they differ only by which schemas exist
in them.

---

## 2. Per-cluster preparation (one-time, run as the master user)

Do this once per Aurora cluster, including `default`. The PostGIS extension
requires `rds_superuser`, so it is created by the master user, not the
application role.

```bash
# Master credentials are used ONLY for this preparation step.
for host in "$DEFAULT_DB_HOST" "$TENANT_1_DB_HOST" "$TENANT_2_DB_HOST"; do
    # 2a. Application database.
    PGPASSWORD=$MASTER_DB_PASSWORD psql -h "$host" -U postgres \
        -c "CREATE DATABASE tenants_back;"

    # 2b. PostGIS extension (required by GIS models in the tenant apps).
    PGPASSWORD=$MASTER_DB_PASSWORD psql -h "$host" -U postgres -d tenants_back \
        -c "CREATE EXTENSION IF NOT EXISTS postgis;"
done
```

> The bootstrap script (`deploy/bootstrap.sh`, Step 1) also runs
> `CREATE EXTENSION IF NOT EXISTS postgis` via `manage.py dbshell` for each
> alias, so this is idempotent — running both is safe.

---

## 3. Per-shard application roles & credentials

Each cluster has its **own** application role and password (a deliberate
decision — see `settings_local.py.example`). A compromised credential for one
shard must not grant access to another.

### 3a. Create the role on each cluster

```sql
-- Run on EACH cluster as the master user. Use a distinct password per cluster.
CREATE ROLE tenants_app_default  LOGIN PASSWORD '<default-secret>';
GRANT ALL PRIVILEGES ON DATABASE tenants_back TO tenants_app_default;
```

The application role needs DDL rights on `tenants_back` because:

- On `default`: `migrate_schemas --shared` creates the `public`-schema tables.
- On `tenant_*`: `migrate_schemas` issues `CREATE SCHEMA` and creates
  per-schema tables when provisioning a `NEW` tenant.

(For a tighter footprint you can grant `CREATE` on the database and ownership
of created schemas rather than `ALL PRIVILEGES`; `ALL` is the simplest correct
default for the MVP.)

### 3b. Distributing the credentials (MVP: manual)

For the MVP we distribute `settings_local.py` by hand. The secrets (per-shard
`HOST`/`USER`/`PASSWORD` and `SECRET_KEY`) live only inside that file:

1. Author `settings_local.py` once from `settings_local.py.example` (§5b),
   filling in the real endpoints and the per-shard passwords from §3a.
2. Copy it to each backend host, readable only by the service user:
   ```bash
   scp settings_local.py backend-1:/tmp/
   ssh backend-1 'sudo install -o ubuntu -g ubuntu -m 600 \
       /tmp/settings_local.py \
       /home/ubuntu/tenants_back/tenants_back/settings_local.py && \
       rm /tmp/settings_local.py'
   ```
3. Restart gunicorn so it picks up the file.

The file is gitignored — keep the master copy in your team's password manager.
For an automated, secrets-never-handed-around alternative (recommended once you
move past the MVP or adopt an Auto Scaling Group) see
**Appendix A — SSM Parameter Store**.

---

## 4. TLS

All production connections use `sslmode=verify-full` against the **vendored**
AWS RDS CA bundle, so no certificate is fetched at deploy time:

- Bundle path: `deploy/certs/aws-rds-global-bundle.pem` (committed to the repo).
- `settings.py` exposes it as `AWS_RDS_CA` (overridable via the `AWS_RDS_CA`
  env var).
- `_aurora_db_options()` builds the per-cluster `OPTIONS`:
  `{"connect_timeout": 5, "sslmode": "verify-full", "sslrootcert": AWS_RDS_CA}`.

To refresh the bundle when AWS rotates CAs:

```bash
curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
     -o deploy/certs/aws-rds-global-bundle.pem
```

---

## 5. Django settings wiring

### 5a. Dev default (`settings.py`)

Only the `default` alias is defined, pointing at a local Postgres. No TLS, no
shards. This is what `runserver` and tests use.

```python
DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME":   "tenants_back",
        "USER":   "postgres",
        "PASSWORD": "postgres",
        "HOST":   "127.0.0.1",
        "PORT":   "5432",
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"connect_timeout": 5},
    },
}
```

### 5b. Production override (`settings_local.py`)

`settings_local.py` re-imports `DATABASES` and `_aurora_db_options`, then
overrides `default` and adds the shards. Shared fields live in a local
`_AURORA_DEFAULTS`; each entry adds its own `HOST`/`USER`/`PASSWORD`
(per-shard credentials). See `settings_local.py.example` for the full template.
For the MVP you author and copy this file manually (§3b); the
`deploy/user_data_backend.sh` + SSM route (Appendix A) generates it
automatically at boot once you adopt it.

> **Critical:** the set of keys in `DATABASES` is the universe of possible
> shard aliases. A `Shard` row can only reference an alias that exists in
> `DATABASES` at process start (`Shard.clean()` enforces this). Adding a shard
> therefore always means: edit `settings_local.py` **first**, restart, **then**
> `sync_shards` (see §8).

---

## 6. Initialization sequence (fresh platform)

Run from **one** backend host, in this exact order. Steps 2–5 are what
`deploy/bootstrap.sh` automates; they are spelled out here so the ordering
constraints (the "under the hood" part) are explicit.

```bash
cd /home/ubuntu/tenants_back && source venv/bin/activate
```

**Step 1 — Generate the tenants app migration (one-time, commit the result).**
```bash
python manage.py makemigrations tenants
```

**Step 2 — SHARED_APPS migrations on `default.public`.**
Creates `tenants_shard`, `tenants_tenant`, `tenants_domain`, `users_user`,
sessions, etc. Must come before anything that reads those tables.
```bash
python manage.py migrate_schemas --shared --database=default
```

**Step 3 — Register shards from `settings.DATABASES`.**
Creates one `Shard` row per alias. `default` is always `is_active=True`;
`--activate` also activates the non-default shards so they show up in the
tenant-creation dropdown.
```bash
python manage.py sync_shards --activate
```

**Step 4 — Create the public `Tenant` + `Domain`.**
django-tenants needs a tenant row for the `public` schema to route the apex
host. This references the default `Shard`, so Step 3 must run first.
```bash
python manage.py shell <<'PY'
from django_tenants.utils import get_public_schema_name
from tenants.models import Tenant, Domain, Shard

pub = Tenant(
    schema_name=get_public_schema_name(),
    name="Public",
    shard=Shard.objects.get(is_default=True),
    status=Tenant.Status.ACTIVE,
)
pub.full_clean()
pub.save()
Domain.objects.create(domain="example.com", tenant=pub, is_primary=True)
PY
```

**Step 5 — Create the first `tenant_admin`.**
Lives in `public.users_user`; can log in at `https://example.com/admin/` and
manage tenants/shards/domains.
```bash
python manage.py shell <<'PY'
from users.models import User
User.objects.create_superuser(
    username="admin", email="admin@example.com",
    password="<record-this>", role=User.Role.TENANT_ADMIN,
)
PY
```

After Step 5 the platform is initialized: the `public` schema exists on
`default`, shards are registered and active, and an admin can create tenants.

> The `bootstrap_public` management command does Steps 4–5 idempotently, and
> `bootstrap_tenant --schema … --shard …` does §7 end-to-end — both are the
> scripted equivalents of the explicit shells above.

---

## 7. Provisioning a business tenant

Creating the `Tenant` row (admin UI or API) only inserts a registry record with
`status=NEW` — **no schema exists yet**. Provisioning is a separate, explicit
step on a backend host.

```bash
# Resolves the shard from Tenant.shard, CREATE SCHEMA for NEW tenants,
# runs TENANT_APPS migrations, flips NEW -> ACTIVE.
python manage.py migrate_schemas --schema_name=acme
```

Expected:
```
=== Shard 'tenant_1': 1 tenant(s) eligible ===
  -> Created schema 'acme' on 'tenant_1'
OK   acme -> active
```

What the router/`allow_migrate` guarantees during this run:

- On `default` / `public` schema → only `SHARED_APPS` tables.
- On a `tenant_*` shard / tenant schema → only `TENANT_APPS` tables.

So `acme`'s `orders_order`, `cars_car`, … are created **only** in
`tenant_1.acme`, never in `public`.

The status transition is an **atomic claim**: `migrate_schemas` does
`UPDATE … WHERE status IN (NEW, ACTIVE, DEACTIVATED) → PENDING` so two
concurrent runs can't migrate the same tenant; on success it finalizes to
`ACTIVE` (or the previous status), on error to `FAILED` with `last_error`.

The first `company_admin` for the tenant is created either via the admin UI
("Create admin" button → `POST /api/tenants/<id>/create-admin/`) or in a shell
using `use_alias(...)` + `schema_context(...)` (see README Step 5).

---

## 8. Adding a shard later (`tenant_3`)

1. Provision the cluster (§2) and create its application role (§3a) and SSM
   params (§3b / Appendix A).
2. Add `DATABASES["tenant_3"]` to `settings_local.py` on **every** backend
   host (use the `_AURORA_DEFAULTS` pattern), then restart gunicorn so the new
   alias is live.
3. Register it:
   ```bash
   python manage.py sync_shards --activate
   ```
   (or add the `Shard` row via admin — the alias dropdown reads
   `settings.DATABASES`).
4. `tenant_3` now appears in the tenant-creation shard dropdown.

Order matters: the alias must exist in `DATABASES` **before** the `Shard` row,
or `Shard.clean()` rejects it.

---

## 9. Connection sizing

Aurora `max_connections` scales with instance memory. Each backend host holds
persistent connections governed by `CONN_MAX_AGE=60` and
`CONN_HEALTH_CHECKS=True`, per alias.

Rough upper bound of connections one cluster sees:

```
backend_hosts × gunicorn_workers   (for that cluster's alias)
```

We run **sync (prefork) workers** — one request per process — so each worker
process holds roughly **one in-use connection per alias at a time**, and
concurrency scales with the number of **worker processes** (`NUM_WORKERS`), not
with threads.

Don't compute a "safe" number on paper and assume it holds — **measure** with
the Aurora `DatabaseConnections` CloudWatch metric under load and keep it
comfortably below `max_connections`. If you approach the ceiling, lower
`CONN_MAX_AGE` or front Aurora with a pooler.

> Pooler caveat: we deliberately skipped RDS Proxy for this stack. A
> transaction-pooling pooler like **PgBouncer** would additionally require
> `DISABLE_SERVER_SIDE_CURSORS = True` in `DATABASES` (server-side cursors
> break when a connection isn't bound for the whole transaction). Revisit only
> if metrics demand it.

---

## 10. Verification & recovery

```bash
# Status of every tenant vs. its real migration state (read-only).
python manage.py reconcile_tenants --report

# Confirm a schema physically exists on its shard.
python manage.py dbshell --database=tenant_1 <<'SQL'
SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'acme';
SQL
```

| Symptom | Action |
|---|---|
| Tenant stuck in `PENDING` | `reconcile_tenants --only-pending` |
| `FAILED` tenant after fixing the cause | set `status=NEW` in admin, then `migrate_schemas --schema_name=<name>` |
| Schema missing for an `ACTIVE`/`DEACTIVATED` tenant | **data-loss territory** — `migrate_schemas` refuses and marks `FAILED`; restore from backup, then `create_tenant_schema <name>` before migrating |
| Need a schema without migrations (restore workflow) | `create_tenant_schema <name>` (DBA-only; guards against `public` and default-shard) |

---

## 11. Backup / restore note

Because tenants are sharded, a logical backup is **per cluster**:

```bash
pg_dump -h "$TENANT_1_DB_HOST" -U tenants_app_tenant1 -d tenants_back \
        -n acme -Fc -f acme.dump          # single tenant schema
```

To restore a schema into a fresh cluster: create the schema shell with
`create_tenant_schema <name>` (or `pg_restore` the dump's schema), ensure the
`Tenant` row points at the right shard, then run `migrate_schemas
--schema_name=<name>` to bring it up to the current migration head.

---

## Appendix A — Automating credential distribution with SSM Parameter Store (recommended for production)

> The same guidance also lives in the README's **Secrets & credential
> distribution** section; repeated here so this DB doc is self-contained.

The manual copy in §3b is fine for one or two hosts, but it doesn't scale and
leaves the secrets sitting in a file you pass around. For production — and
**mandatory** once you use an Auto Scaling Group, where new hosts boot with no
human in the loop — store the secrets in AWS SSM Parameter Store and let each
instance fetch them at first boot. Nothing in the application code changes; only
where `settings_local.py` comes from.

### A.1 Store one parameter per secret

`SecureString` parameters are encrypted at rest with KMS:

```bash
aws ssm put-parameter --type SecureString --name /tenants/django_secret       --value "$(openssl rand -hex 50)"

aws ssm put-parameter --type String       --name /tenants/default/db_user      --value 'tenants_app_default'
aws ssm put-parameter --type SecureString --name /tenants/default/db_password  --value '<default-secret>'
# ... repeat for tenant_1, tenant_2
```

Resulting layout:
```
/tenants/django_secret                       (SecureString)
/tenants/default/db_user      /tenants/default/db_password
/tenants/tenant_1/db_user     /tenants/tenant_1/db_password
/tenants/tenant_2/db_user     /tenants/tenant_2/db_password
```

### A.2 Grant the backend EC2 IAM role read access

The instance role must allow reading those parameters (and decrypting the KMS
key), scoped to `/tenants/*`:

```json
{
  "Effect": "Allow",
  "Action": ["ssm:GetParameter", "ssm:GetParameters"],
  "Resource": "arn:aws:ssm:<region>:<account>:parameter/tenants/*"
}
```

No AWS access keys are baked into the host — the instance authenticates as
itself via its attached role.

### A.3 Fetch at boot and generate settings_local.py

This is exactly what `deploy/user_data_backend.sh` already does: for each name
it calls `aws ssm get-parameter --with-decryption` and writes the resulting
`settings_local.py`. To adopt SSM, drop the manual §3b step and run that script
as EC2 user-data — provisioning then becomes fully hands-off.
