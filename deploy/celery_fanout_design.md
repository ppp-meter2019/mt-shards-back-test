# Celery: fanout dispatch + universal tasks (standalone ⇄ multi-tenant)

Status: design (consolidated). Companion to `deploy/standalone_multitenant_design.md`
(this is the Celery step deferred there).

Goal: one code base where **both scheduled and ad-hoc tasks** behave correctly in
multi-tenant (fanout across tenants) and standalone (run once), driven by a schedule
**dict in settings**, with the `django-celery-beat` DB dropped.

---

## Mental model (the moving parts, in one place)

The fanout scheduler stacks several deliberate — but individually non-obvious — mechanisms.
Know these six and the rest reads straightforwardly:

1. **Two-sided contract across a one-way layer boundary.** `commons.platform.beat`
   (mode-agnostic; PRODUCES beat entries at settings-load) and `tenants.celery.dispatch`
   (MT-only; DEFINES the tasks) never import each other's code — they share only the string
   constant `FANOUT_TASK_NAME` (declared in commons, imported UP by tenants). Enforced by E005.
2. **Schedule wrapper `scoped_schedule`.** Every `CELERY_BEAT_SCHEDULE` entry is wrapped; under
   MT a per-tenant entry is rewritten to fire `fanout_dispatch` instead of the real task
   (identity in standalone / for `scope="public"`). tz-awareness is derived from the schedule
   TYPE (crontab = calendar/per-tenant-local; number/timedelta = interval/all-tenants).
3. **`scope` rides as an ATTRIBUTE on a `dict` subclass (`SchedEntry`)** — invisible to Celery's
   `**entry`, yet readable by the E003 check. (`{**entry}` / `dict()` drop it — never spread.)
4. **The task name lies about its module path on purpose.** `fanout_dispatch` / `sub_dispatch`
   live in `tenants/celery/dispatch.py` but register as `tenants.tasks.*` (via `name=`) — a
   stable contract that survived the code move; wired into autodiscover by a bottom import in
   `tenants/tasks.py`.
5. **Level-triggered per-tenant tz due-check.** Calendar fanout considers only the latest past
   occurrence per tenant and fires it iff it is newer than that tenant's `TaskRun` watermark
   AND within `grace` — self-heals a missed tick within `grace`, never fires late, never N times.
6. **Two-level fanout tree.** `fanout_dispatch` -> batched `sub_dispatch` (BATCH_SIZE) ->
   per-tenant `send_task(headers={_schema_name})`, so ~1000 enqueues don't serialize on one task.

Config knobs split into LOAD-TIME (`FANOUT_PERIOD_DEFAULT`, via env/settings_mode) vs RUNTIME
(`TENANT_BEAT` -> `BEAT_DEFAULTS`) — see "Configuration tiers" in the dual-mode doc.

---

## 0. Invariants

- Schedule lives in `CELERY_BEAT_SCHEDULE` (settings dict); each entry is wrapped by
  `scoped_schedule(entry, scope=…)`, keeping the **entry's normal shape** (`task`, `schedule`,
  `args`, `kwargs`, `options`).
- **tz-awareness is derived from the schedule TYPE**, never a manual flag:
  `crontab` → per-tenant local time; interval (number / `timedelta` / `schedule`) →
  tz-irrelevant, fan to all. String / `solar` / `clocked` → hard error.
- Beat stays a **stock scheduler**; the dispatcher is an ordinary scheduled task.
- The existing per-task tenant machinery (`_schema_name` header → `TenantTask.__call__`) is
  **reused unchanged**.
- **Standalone: `scoped_schedule` is the identity** → zero behavioral change; fanout/tz/queues/
  RedBeat activate only under `USE_MULTITENANT=True`.

---

## 1. Schedule wrapper `scoped_schedule` + `_classify_schedule`  (`commons/platform/beat.py`)

```python
import numbers
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from celery.schedules import crontab, schedule as interval_schedule

def _classify_schedule(sched):
    if isinstance(sched, (numbers.Number, timedelta, interval_schedule)):
        return "interval", sched, None
    if isinstance(sched, crontab):
        return "calendar", sched, _crontab_to_cronspec(sched)      # crontab → "0 8 * * *"
    raise ImproperlyConfigured(
        f"scoped_schedule: unsupported schedule {type(sched).__name__}. Allowed: number/"
        f"timedelta/schedule (interval) or crontab (calendar). Strings/solar/clocked "
        f"are rejected; one-off runs go through apply_async(eta=...).")

def scoped_schedule(entry, *, scope="tenants", fanout_period=60, grace=None):
    entry = dict(entry)
    if not settings.USE_MULTITENANT or scope == "public":
        return entry                                               # passthrough (stock beat)
    kind, sched, cron = _classify_schedule(entry["schedule"])
    disp = {"task_name": entry["task"], "scope": scope,
            "task_args": list(entry.get("args", ())),
            "task_kwargs": entry.get("kwargs", {}),
            "task_options": entry.get("options", {})}
    if kind == "calendar":                                         # per-tenant tz
        disp["cron"] = cron
        if grace is not None:
            disp["grace"] = grace                                  # else runtime default (TENANT_BEAT)
        beat_schedule = float(fanout_period)                       # beat tick = precision
    else:                                                          # interval → fan-to-all at cadence
        beat_schedule = sched
    return {"task": "tenants.tasks.fanout_dispatch", "schedule": beat_schedule, "kwargs": disp}
```

`scope ∈ {public, tenants}` (no `both` — use two entries if ever needed).

Usage (entry shape preserved):
```python
CELERY_BEAT_SCHEDULE = {
    "push-route-changes": scoped_schedule({"task": "apps.routes.tasks.push_changes",
                                  "schedule": 5.0}, scope="tenants"),            # interval → all
    "send-daily-summary": scoped_schedule({"task": "apps.reports.tasks.daily",
                                  "schedule": crontab(minute=0, hour=8)}, scope="tenants"),  # 08:00 local
    "resolve-gate-reconcile": scoped_schedule({"task": "tenants.tasks.reconcile_host_registry_task",
                                      "schedule": crontab(minute=0, hour=4)}, scope="public"),
}
```

---

## 2. Universal dispatcher + sub-dispatcher  (`tenants/celery/dispatch.py`, MT-only)

Task NAMES are kept stable (`tenants.tasks.fanout_dispatch` / `.sub_dispatch` via `name=`)
even though the code lives in `tenants/celery/dispatch.py`; registered by an import at the
bottom of `tenants/tasks.py` (which Celery autodiscover loads).

Beat fires ONE `fanout_dispatch` per due entry; it splits the due set into batches and
hands each to `sub_dispatch`, which run in parallel on the `fanout` queue. This solves
the "one flat fanout of 1000 enqueues ≈ 4 s" problem (parallel enqueue), while beat
stays O(#schedules).

```python
@shared_task(base=Task, queue=task_queue("fanout"), acks_late=True, max_retries=0)
def fanout_dispatch(task_name, scope="tenants", cron=None, grace=None,
                    task_args=None, task_kwargs=None, task_options=None, batch_size=None):
    if not _acquire_lock(task_name, task_args):        # overlap-lock, key = (task, args)
        return {"skipped": "overlapping"}
    now = timezone.now()
    batch_size = batch_size or _beat_conf("BATCH_SIZE")
    if cron:                                           # calendar → per-tenant tz filter
        grace = grace if grace is not None else _beat_conf("TZ_GRACE_SECONDS")
        due = _due_by_tenant_tz(task_name, task_args, cron, now, grace)
        run_ts = now
    else:                                              # interval → all ACTIVE (tz-agnostic)
        due = list(active_target_schemas(scope))
        run_ts = None
    for i in range(0, len(due), batch_size):
        sub_dispatch.delay(task_name, due[i:i+batch_size], task_args, task_kwargs,
                           task_options, run_ts.isoformat() if run_ts else None)
    return {"fanned_out": len(due)}

@shared_task(base=Task, queue=task_queue("fanout"), acks_late=True, max_retries=0)
def sub_dispatch(task_name, schemas, task_args=None, task_kwargs=None,
                 task_options=None, run_ts=None):
    app, sent = current_app, []
    for schema in schemas:
        try:
            app.send_task(task_name, args=task_args or [], kwargs=task_kwargs or {},
                          headers={"_schema_name": schema}, **(task_options or {}))
            sent.append(schema)                        # mark only successful sends
        except Exception:
            logger.exception("sub_dispatch: send failed for %s (retried next tick)", schema)
    if run_ts and sent:                                # calendar only; at-least-once
        TaskRun.mark_ran(task_name, args_sig, sent, run_ts)
    return {"sent": len(sent), "requested": len(schemas)}
```

The real per-tenant task runs via the existing machinery: `_schema_name` header →
`TenantTask.__call__` (task.py) enters the tenant's shard+schema for exactly that
invocation, in a `with tenant_context(...)` block whose `finally` restores both axes
whether the task returns or raises. Unchanged.

Deliberately NOT a `task_prerun`/`task_postrun` pair: two signals share no `finally`, so
the context manager would have to be stashed on the task SINGLETON between them — neither
crash- nor pool-safe. See the docstrings in `tenants/celery/app.py` and `task.py`.

`public` scope: `scoped_schedule` passthrough → beat sends the real task once (public context,
`CELERY_TIMEZONE`), never through the dispatcher.

---

## 3. Per-tenant timezone  (`Tenant.timezone` + `TaskRun`, MT-only)

```python
class Tenant(...):
    # NULL = no-op sentinel set at tenant creation. The real value is pushed by an
    # in-schema SINGLETON (arrives at merge) via sync_tenant_timezone(); while NULL,
    # calendar tasks are NOT enqueued for this tenant.
    timezone = models.CharField(max_length=64, null=True, blank=True, default=None,
                                validators=[_validate_iana_tz])   # validated only when non-null

def sync_tenant_timezone(schema_name, tz):                        # hook for the future singleton
    Tenant.objects.filter(schema_name=schema_name).update(timezone=tz)   # writes default.public

class TaskRun(models.Model):                                      # default.public watermark
    schema = models.CharField(max_length=63)
    task = models.CharField(max_length=255)
    args_sig = models.CharField(max_length=12, blank=True)        # _argsig(task_args), "" if none
    last_run_at = models.DateTimeField()
    class Meta: unique_together = [("schema", "task", "args_sig")]
    # load_map(task, args_sig) -> {schema: last_run_at}
    # mark_ran(task, args_sig, schemas, run_ts) bulk-upsert
```

`args_sig` is part of the identity, not decoration. Two calendar entries may share a task
NAME and differ only by args — `fetch(1)` at 08:00 and `fetch(7)` at 09:00 are independent
schedules, which is why the overlap-lock keys on `(task, _argsig(args))`. The watermark must
agree: keyed by task name alone, the wave that lands second reads the first one's watermark,
sees the occurrence as already run, and is skipped indefinitely — silently, because the two
never contend for the lock. One signature is computed per wave in `fanout_dispatch` and
threaded through the lock, the due-check and `sub_dispatch`, so the three cannot disagree.

`_argsig` returns `""` for an entry with no args rather than a digest of `"[]"`. Most
entries take none, so a digest there would be a constant stamped on nearly every row and
lock key while distinguishing nothing — and these rows are read by hand when asking why a
task did not fire for a tenant. Uniqueness is unaffected (`""` cannot collide with 12 hex
chars), and migration `0008_taskrun_args_sig` backfills existing rows with `""`, which is
the identity they already had.

Enumeration helpers (`commons/platform/tenancy.py`), both EXCLUDE the public schema:
```python
def active_target_schemas(scope):                # interval tenant tasks — tz-agnostic
    return (Tenant.objects.filter(status=ACTIVE)
            .exclude(schema_name=get_public_schema_name())
            .values_list("schema_name", flat=True).iterator(chunk_size=500))

def active_tenants_with_tz():                    # calendar tasks — only configured tz
    return (Tenant.objects.filter(status=ACTIVE, timezone__isnull=False)
            .exclude(schema_name=get_public_schema_name())
            .values_list("schema_name", "timezone").iterator(chunk_size=500))
```

Due decision — **latest occurrence + grace window** (no unbounded catch-up, so
interdependent tasks are never fired late/out of order):
```python
def _due_by_tenant_tz(task_name, args_sig, cron, now, grace):
    last, due = TaskRun.load_map(task_name, args_sig), []
    for schema, tzname in active_tenants_with_tz():
        tz = ZoneInfo(tzname)
        t_fire = croniter(cron, now.astimezone(tz)).get_prev(datetime).astimezone(utc)  # last past fire
        last_run = last.get(schema)
        already  = last_run is not None and t_fire <= last_run
        too_late = (now - t_fire).total_seconds() > grace                                # skip if stale
        if not already and not too_late:
            due.append(schema)
    return due
```
- **DST:** evaluated in `ZoneInfo(tenant.tz)`; fall-back double hour is deduped by
  `TaskRun`; spring-forward gap time is a documented edge (avoid scheduling in the local
  DST window ~02:00–03:00).
- Skipped tenants (NULL tz) are logged per tick (count) for observability.

### 3.1 `grace` — the self-heal / max-lateness bound (first-class knob)

A missed occurrence is fired late **only within `grace`**; beyond it, the occurrence is
**dropped** (wait for the next scheduled time). This is what makes self-heal *bounded* —
critical because calendar tasks can be interdependent, and a very-late run could execute
out of order.

- Rule (in `_due_by_tenant_tz`): fire iff `t_fire > last_run` **AND** `now - t_fire ≤ grace`.
- `grace ≥ fanout_period` (a beat tick must not fall outside the window of a fresh
  occurrence). Enforced by a Django system check `beat.E00x`.
- Example (`send-daily-summary` at 08:00, `grace=300`): first tick after an outage at
  08:07 → 2 min ≤ 5 min → **fire**; at 08:12 → 7 min > 5 min → **skip**, run tomorrow 08:00.
- Effects: no retroactive fire on deploy (a task whose time already passed by more than
  `grace` is skipped); never N fires for N missed days (only the latest occurrence is
  considered).
- Config: `grace = TENANT_BEAT["TZ_GRACE_SECONDS"]` (default 300) + per-task override
  `scoped_schedule(entry, grace=…)`, resolved **at runtime** in the dispatcher (per-task → setting →
  in-code default). Applies to **calendar** tasks only (interval tasks have no watermark).

**Public / default tenant:** covered by `CELERY_TIMEZONE` (stock beat), NOT per-tenant tz.
No tooling needed to set its tz; it is excluded from tenant enumeration above.

---

## 4. Ad-hoc tasks + `celery.py` mode gate

Ad-hoc `@shared_task`s keep the existing MT machinery (`celery/app.py`, `task.py`); in
standalone they run against the single DB with no header/context, via the bootstrap gate:

```python
# tenants_back/celery.py
if settings.USE_MULTITENANT:
    from tenants.celery import CeleryApp; app = CeleryApp("tenants_back")   # shard-aware
else:
    from celery import Celery;            app = Celery("tenants_back")      # plain
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```
This also removes the standalone-boot import of `tenants.celery`/`django_tenants`, making
`django-tenants` truly optional in standalone.

---

## 5. Queues — task-level with standalone fallback

Queue is declared at the task via a mode-aware helper; **all** queue/route config is
MT-only. In standalone every task goes to the effective default queue (host workers
serve it).

```python
# commons/platform/beat.py  (task_queue lives alongside scoped_schedule)
def task_queue(name):
    """MT → given queue; standalone → None → Celery uses the effective
    task_default_queue (host's CELERY_TASK_DEFAULT_QUEUE or Celery's built-in default)."""
    return name if settings.USE_MULTITENANT else None
```
`queue=None` ⇒ "no queue specified" ⇒ Celery routes to `task_default_queue` (verified:
Celery docs + a probe on Celery 5.6.3). No hardcoded queue name, no reliance on routing
precedence; resolution deferred to send time.

```python
if USE_MULTITENANT:
    from kombu import Queue
    CELERY_TASK_QUEUES = [Queue("fast"), Queue("slow"), Queue("service"), Queue("fanout")]
    CELERY_TASK_DEFAULT_QUEUE = "fast"
# standalone: none set → default queue "celery", existing workers (no -Q) serve all.
```
Golden rule: business/shared tasks always wrap the queue with `task_queue(...)`, so the
standalone fallback cannot leak.

---

## 6. HA beat via RedBeat — MT-only

Beat must be a singleton (SPOF); HA = redundancy without duplicate firing.

- MT: `CELERY_BEAT_SCHEDULER = "redbeat.RedBeatScheduler"` (MT branch). RedBeat reads our
  `CELERY_BEAT_SCHEDULE` (→ `app.conf.beat_schedule`, its `RedBeatConfig.schedule`
  property) and its own `CELERY_REDBEAT_*` settings from `app.conf` (Celery's namespace
  generically carries **all** `CELERY_*` keys into `app.conf` — verified by source +
  probe). A Redis lock lets multiple beat replicas run safely: one active, standbys take
  over on failure; a deposed node exits and is restarted by the supervisor.
- standalone: `CELERY_BEAT_SCHEDULER` NOT set → stock `PersistentScheduler` (or the host
  project's own). No RedBeat, no lock, no HA imposed.
- `celery-redbeat` stays in the single universal `requirements.txt`, **inert** in
  standalone (activated only by the MT-only scheduler setting — same as `django-tenants`).
- RedBeat Redis must be **noeviction** (broker URL by default, or a dedicated instance in
  `settings_local.py`); never the eviction `tenant_resolve` cache.

Layered dedup (makes HA safe): RedBeat lock → dispatcher overlap-lock → `TaskRun` +
idempotent tasks absorb any rare double-fire.

---

## 7. Deployment / launch

- **Beat (MT):** `celery -A tenants_back beat -l info` (scheduler from the setting). For HA
  run 2–3 identical replicas under a supervisor (systemd `numprocs`>1 / k8s
  `replicas: 2-3`); the RedBeat lock elects one active. No `celerybeat-schedule` file
  (state is in Redis). **Never** embedded `worker -B` in prod.
- **Workers (MT):** per queue — `-Q fast` / `-Q slow` / `-Q service` / `-Q fanout`.
- **standalone:** beat as the host runs it (single, stock); worker without `-Q`
  (serves the default `celery` queue).
- `deploy/supervisor_celery.conf` to be updated: beat replicas + a `fanout`-queue worker.

---

## 8. Drop `django-celery-beat` DB

Schedule lives in settings → `PeriodicTask`/`*Schedule` tables would be permanently empty
(pure per-schema migration overhead). No live `PeriodicTask` rows exist → clean drop, no
cutover. Remove:
- Files: `celery/db_scheduler.py`, `celery/change_marker.py`,
  `management/commands/resync_beat_schedules.py`.
- `settings.py`: `django_celery_beat` from `_THIRD_PARTY_APPS`; `CACHES["beat"]`;
  `BEAT_MARKER_TTL_SECONDS`, `DJANGO_CELERY_BEAT_MAX_LOOP_INTERVAL`; the DB `CELERY_BEAT_SCHEDULER`.
- `tenants/admin.py`: `PeriodicTask/*Schedule` registration.
- Marker-bump signals (`signals.py`/`views.py`/`migrate_schemas`/`reconcile_tenants`) —
  no DB schedule to invalidate; tenant freshness is decided by the dispatcher at fanout.
- `requirements.txt`: `django-celery-beat`.
- `TENANT_COMMANDS` guard list + change_marker/db_scheduler tests.

---

## 9. Reliability / failure modes

- **Delivery = at-least-once** for calendar tasks: `TaskRun.mark_ran` AFTER send, in
  `sub_dispatch`, per successfully-sent tenant, with the shared tick `run_ts`. Real tz
  tasks must be **idempotent** (optional dedup key `f"{task}:{schema}:{fire_date}"`).
  Exactly-once is not offered (would need an outbox).
- **Broker down** → tick skipped, recovered next tick (level-triggered); never fire-all.
- **Overlap-lock** per `(task, args)` → no piled-up waves; a stale/late calendar
  occurrence is dropped by the grace window. The lock lives in the MT-only `beat_lock`
  cache = the BROKER Redis (NOEVICTION — a lock is never evicted mid-wave, unlike the
  allkeys-lru app `default` cache), static `beatlock:` prefix (tenant-agnostic). It runs
  with **IGNORE_EXCEPTIONS OFF** (unlike the fail-open `tenant_resolve` cache): a coordination
  lock is not disposable, so if the lock Redis is DOWN, `cache.add` RAISES and `fanout_dispatch`
  **fails loudly** (a visible task failure) rather than silently skipping the tick — the outage
  surfaces instead of hiding. (Short 1s socket timeouts keep the failure fast.)
- Dispatcher enumerates from the DB (source of truth), not a Redis snapshot → no stampede.

---

## 10. Settings surface (MT-only additions)

```python
if USE_MULTITENANT:
    CELERY_BEAT_SCHEDULER = "redbeat.RedBeatScheduler"
    CELERY_REDBEAT_KEY_PREFIX = "redbeat:"
    CELERY_REDBEAT_LOCK_TIMEOUT = ...            # ≥ beat loop interval; tune
    # CELERY_REDBEAT_REDIS_URL -> settings_local (prod); default = broker_url
    CELERY_TASK_QUEUES = [Queue("fast"), Queue("slow"), Queue("service"), Queue("fanout")]
    CELERY_TASK_DEFAULT_QUEUE = "fast"
    TENANT_BEAT = {"TZ_GRACE_SECONDS": 300, "BATCH_SIZE": 100}   # runtime knobs (+ in-code DEFAULTS)
    CELERY_BEAT_SCHEDULE = { "resolve-gate-reconcile": scoped_schedule(...) , ... }
# standalone: none of the above; stock scheduler, default queue, scoped_schedule = identity.
```
New dependency: `croniter` (universal requirements; used only by the MT tz path).

---

## 11. Mode matrix (Celery)

| | multi-tenant | standalone |
|---|---|---|
| `scoped_schedule` | wraps (fanout / tz) | identity (passthrough) |
| scheduler | `redbeat.RedBeatScheduler` (HA) | stock / host |
| calendar task | per-tenant tz (`Tenant.timezone`, `TaskRun`, grace) | stock crontab in `CELERY_TIMEZONE` |
| interval task | `fanout_dispatch` → all ACTIVE | runs once |
| public task | once in public | once |
| queues | `fast/slow/service/fanout` (`task_queue`) | default `celery` |
| beat HA | replicas + Redis lock | host's concern |
| redbeat / django-tenants | active | installed, inert |
| beat-DB (`django_celery_beat`) | removed | removed |

---

## 12. Phases

- **A. Core — DONE** (units built + tested; live beat scheduler NOT switched yet —
  cutover is D/E). Added: `commons/platform/beat.py` (`task_queue` + `scoped_schedule`)
  (`scoped_schedule`, `_classify_schedule`, `_crontab_to_cronspec`, `beat_conf`, `BEAT_DEFAULTS`),
  `commons/platform/tenancy.py::active_target_schemas`, `tenants/tasks.py`
  (`fanout_dispatch` interval+public / `sub_dispatch` / `_acquire_lock` / `_argsig`;
  `provision_tenant`/`drop_tenant_schema_task` now `queue=task_queue("service")`), settings
  MT-only queues (`fast/slow/service/fanout`, dropped `CELERY_TASK_ROUTES`) + `TENANT_BEAT`.
  Tests: `tenants/tests/test_fanout.py` (20). The calendar/tz branch of `fanout_dispatch`
  raises `NotImplementedError` until Phase B. `CELERY_BEAT_SCHEDULE` and the scheduler
  swap are NOT added yet (still the DB scheduler) — no cutover in A.
- **B. tz — DONE.** `Tenant.timezone` (NULL-sentinel + `_validate_timezone`), `TaskRun`
  (+ `load_map`/`mark_ran`), `sync_tenant_timezone`, migration `0006`; `_due_by_tenant_tz`
  (latest-occurrence + grace, level-triggered) wired into `fanout_dispatch`'s calendar
  branch; `sub_dispatch` marks `TaskRun` after send (calendar only, at-least-once);
  `active_tenants_with_tz` (excludes public + NULL tz); `croniter` dep; grace check is
  `tenants.E002` (grace ≥ fanout_period); `tenants.E006` (no two fanout entries share
  `(task_name, args)` — that pair IS the overlap-lock key, so such entries suppress each
  other, and if they differ in cron/interval/grace the applied grace depends on which wave
  wins the lock). Tests: `test_fanout.py` (31 total) + `TaskRunTests`
  in `db_integration.py` (DB harness). MT suite 178. Live cutover still deferred to D/E.
- **C. Gate — DONE.** `tenants_back/celery.py` is mode-aware: MT → `tenants.celery.CeleryApp`
  (shard-aware); standalone → plain `celery.Celery`. Verified by probe: in standalone the
  app is `celery.app.base.Celery` and `tenants` is not imported at all — so django-tenants
  is not needed to boot Celery there. Still no live scheduler cutover.
- **D. Demolition + cutover — DONE.** Deleted `db_scheduler.py`, `scheduler.py`,
  `change_marker.py`, `resync_beat_schedules`, `test_change_marker.py`. Removed
  `django_celery_beat` from `_THIRD_PARTY_APPS`+`TENANT_APPS`, `CACHES["beat"]`,
  `BEAT_MARKER_*`, the PeriodicTask admin, and the marker-bump signals/calls
  (`signals.py`, `views.py`, `migrate_schemas` `--no-beat-notify`/`_beat_notify`,
  `reconcile_tenants`); dropped `django-celery-beat` from requirements. **Cutover:** MT
  now runs stock `celery.beat:PersistentScheduler` off `CELERY_BEAT_SCHEDULE`
  (seeded with `resolve-gate-reconcile`); the DB scheduler is gone. Also removed the
  now-dead `Tenant.from_db`/`_loaded_status`. Verified: MT 177 tests, `check` clean,
  `makemigrations --check` clean, beat config loads without `django_celery_beat`;
  `db_integration.py::FanoutTargetsDBTests` covers the enumeration. Mode resolver
  extracted to `commons/platform/mode.py::use_multitenant()` (settings-load-safe; used by
  settings.py and `scoped_schedule`).
- **E. HA — DONE.** `celery-redbeat` added (requirements). MT branch:
  `CELERY_BEAT_SCHEDULER="redbeat.RedBeatScheduler"`, `CELERY_REDBEAT_KEY_PREFIX`,
  `CELERY_REDBEAT_LOCK_TIMEOUT=90`, `CELERY_REDBEAT_REDIS_URL` (env `REDBEAT_REDIS_URL` →
  broker; override to a dedicated noeviction Redis in settings_local). `celery.py` sets
  `app.conf.redbeat_redis_url` directly in MT so RedBeat's `in`-based `is_key_in_conf`
  sees it (namespace-loaded keys aren't reported by `in`) — silences a spurious
  "set redbeat_redis_url explicitly" deprecation. `supervisor_celery.conf` updated: added
  a `fanout`-queue worker and documented running the beat program on multiple hosts for
  HA (RedBeat lock elects one active). Verified: `RedBeatConfig` reads our
  CELERY_BEAT_SCHEDULE + resolves redis_url with no deprecation; standalone leaves the
  scheduler unset.
- Tests both modes; standalone boot without `django_celery_beat`. ALL PHASES A–E DONE.

---

## 13. Tests

- `_classify_schedule`: interval/crontab ok; str/solar/clocked → `ImproperlyConfigured`.
- `scoped_schedule`: standalone → identity; MT interval → `fanout_dispatch` same cadence; MT
  crontab → `fanout_dispatch` with `schedule=fanout_period` + `cron`; public → passthrough;
  args/kwargs/options forwarded.
- `scope_of` / `tenants.E003`: `scoped_schedule` returns a `SchedEntry(dict)` carrying the
  scope as an ATTRIBUTE (Celery reads the entry as a plain mapping via `**entry`, so the attr
  is invisible — no unknown-key error); `scope_of(entry)=getattr(entry,"scope",None)`. The
  check flags any `CELERY_BEAT_SCHEDULE` entry that bypassed `scoped_schedule` (scope_of is
  None) so per-tenant tasks aren't silently left un-fanned-out. Base
  `settings_base.py` carries a worked `scoped_schedule` example; `CELERY_BEAT_SCHEDULE` is a shared `{}`
  the MT layer augments (never replaces).
- `fanout_dispatch`: interval → all ACTIVE; cron → due subset (mock tz + TaskRun);
  overlap-lock; two entries same task / different args don't collide.
- `sub_dispatch`: N schemas → N `send_task` with correct `_schema_name`; send-fail →
  not marked → retried next tick.
- `_due_by_tenant_tz`: two tenants, different tz → due at different UTC ticks; missed tick
  within grace self-heals; beyond grace skips; no retroactive fire.
- `task_queue`: MT → name; standalone → None.
- Ad-hoc tasks: `headers_with_schema`/`TenantTask.__call__` unchanged.
- standalone: beat config loads without `django_celery_beat`.
