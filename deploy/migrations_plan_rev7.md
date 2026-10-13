# План: міграції тенантів без простою (редакція 7)

**Дата:** 2026-10-10, оновлено 2026-10-12 (правило вибору шарду, §5.3; `--summary-json`, §9.10; `migrate_schemas_legacy`, §15.4; обґрунтування B1 виправлено за перевіркою на коді, §5.4). **Статус:** план погоджено повністю (включно з §22); усі кроки 1–9 §21 реалізовано 2026-10-12 (крок 3: лог-файли лише з CLI, §9.9; `--tenant` лише на активних шардах, §9.3; legacy — копія без запобіжника, §15.4; крок 4: уточнення реалізації в §9.6, §9.7, §9.10; крок 5: уточнення в §10, §11; крок 6: канарка в окремих тестових налаштуваннях, §20.1; крок 7: уточнення в §5.5, §14, §15.1; крок 8: документація, §18; крок 9: Docker smoke пройдено, §20.5). Кожен крок реалізації йде лише після окремого «так».

**Самодостатність.** Документ не потребує ред. 6: усі фрагменти, на які він раніше посилався (`code_older_than_db`, `invalid_indexes`, `migration_ops`, override `migrate`, канарка, `LockTimeoutE2E`), вставлено сюди. Згадки ред. 6 лишились лише як історія змін. Копія в репозиторії: `deploy/migrations_plan_rev7.md`.

**Звідки взято.**
- Редакція 6 (артефакт WwsVPKeL97pxx7SqdFToFo, `plan_rev6.md`).
- Багатоагентний перегляд (прогін `wf_5003f30f-717`): три архітектори, дебати, синтез, фінальний рецензент.
- Рішення, ухвалені з користувачем 2026-10-10. Їх зведено в §1.
- Посилання `файл:рядок` перевірено на поточному коді 2026-10-10. Перед реалізацією їх треба звірити ще раз.

**Головне в одному абзаці.** Ред. 6 спиралася на сесійний advisory lock на окремому з'єднанні через RDS Proxy. Звідти heartbeat, `DISCARD ALL`, скасування бекендів за `application_name`, перевірки V1–V6 на staging і 13 модулів. Ред. 7 замінює все це рядком оренди `MigrationLease` у таблиці і огородженим CAS. Від проксі нічого не залежить. Мігратор — один модуль і три команди. Функціональність ред. 6 збережено. Нове: статус `MAINTENANCE` для ручних робіт і окремий лог-файл міграції для кожного тенанта.

---

## 0. Що змінилося порівняно з ред. 6

| Ред. 6 | Ред. 7 |
|---|---|
| 13 модулів у `tenants/migrator/` + `bootstrap` + `scripts/proxy_verify.py` | один модуль `tenants/migrator.py` + команди `migrate_schemas`, `migration_lease`, `reconcile_tenants` + `commons/platform/migration_ops.py` |
| Advisory lock на окремому lock-з'єднанні, heartbeat-потік, `still_held()` через `pg_locks`, блокуючий `shared_lock` на 300 с | рядок `MigrationLease` + огороджений CAS; `--shared` — оренда з ключем 0, без очікування |
| `conn.py` (`DISCARD ALL`, `close_clean`) | `_fresh()` і закриття з'єднання шарду в `finally` (§7) |
| `ddl.py`: receiver `connection_created` + префікси `application_name` для скасування | `_open_shard`: `set_config` на кожен тенант; `application_name='tm:<pk>'` лише для діагностики й перевірки M1 |
| `cancel_worker_backends`, cancel-потоки | немає; шкоду обмежує `lock_timeout` |
| `derive_outcome`, settle-цикл, `HARD_ABORT_SETTLE_S`, UNKNOWN | знімок стану з БД після аварійної зупинки |
| 6 Outcome × ~30 reason, коди 0/1/2/3 | `Result(kind ∈ ok/noop/failed/skipped, msg)`, коди 0/1 |
| `StopController`, `MigrationScheduler`, буфер виводу в порядку відправки, `Result.lines` | прапорець `stopping`, чиста функція `pick_next`, вивід без буфера + лог-файли тенантів |
| spawn + `bootstrap` + `db_names` | fork + `_worker_init` (`PR_SET_PDEATHSIG`) |
| Checks E020–E028, W025/W026 | один модуль checks (5 перевірок) + CI-лінт міграцій |
| Автовиправлення в `reconcile_tenants` | `reconcile_tenants` тільки читає |
| V1–V6 на staging, Q6–Q7, Q10–Q12 | немає |
| ≈2500–3000 рядків, ~45 тест-класів, 9 smoke-сценаріїв, staging | ≈1000–1100 рядків, ~21 тест-клас, 5 smoke-сценаріїв, без staging |

Розміри — оцінки: обидві редакції існують лише як план.

---

## 1. Рішення

### 1.1 Перенесені з ред. 6 (без змін)
1. **Порядок деплою:** `--shared` → `--tenant` → `reconcile_tenants --check` → викатка коду. Міграції лише розширювальні (expand-only), плюс фіча-флаги.
2. **PENDING — лише перше провізіонування:** NEW → PENDING → ACTIVE/FAILED. Веб віддає 503 для NEW і PENDING.
3. **Невдале оновлення не змінює статус і не лишає маркера.** Правда — це незастосовані міграції в схемі. Єдиний виняток — `schema_missing` (рішення 1.2.1).
4. **`--keep-going` + `--max-failures=N`:** N падінь поспіль на будь-якому шарді зупиняють увесь прогін.
5. **Гейт деплою — `reconcile_tenants --check`.** Стопер реактивації робить ту саму перевірку для одного тенанта.
6. **Паралельність власна.** Налаштування `TENANT_MIGRATE_JOBS=1` і `TENANT_MIGRATE_JOBS_PER_SHARD=1`, CLI `--jobs` і `--jobs-per-shard`. Заборонено `--executor`, env `EXECUTOR`, `TENANT_MULTIPROCESSING_*`.
7. **`manage.py migrate` у MT заборонено**, крім створення тестових БД. У standalone — звичайний Django `migrate`.
8. **`previous_status` — довідкове поле.**
9. **Перехідних релізів немає:** робочого середовища ще немає.
10. **Код синхронний.**

### 1.2 Ухвалені 2026-10-10
1. **Q1. `schema_missing` при оновленні → FAILED.** Огороджений CAS + `forget_tenant`. Це єдиний виняток з правила 1.1.3: він дає вихід через адмінку FAILED → NEW. Без нього з ACTIVE не можна потрапити в NEW: матриця адмінки дозволяє NEW лише з FAILED.
2. **Q2. Коди виходу — 0 і 1** для `migrate_schemas` і `reconcile_tenants --check`. Причину видно з рядка `Stop:` і підсумку. Розширити можна пізніше.
3. **Q3. Міграцій, що одночасно чіпають схему тенанта і `public`, у проєкті немає** (правило). Тому 55P03 при провізіонуванні не має окремої гілки і дає FAILED. Обрив з'єднання при провізіонуванні лишає PENDING.
4. **Q4. Лічильник `--max-failures` рахує в порядку завершення.** Це замінює «порядок відправки» з ред. 6. Втрату детермінізму прийнято: це запобіжник, і він має реагувати швидко.
5. **Q5. `reconcile_tenants` тільки читає.** Усі автовиправлення прибрано. `migrate_schemas --plan` заборонено з підказкою `reconcile_tenants --report --plan`.
6. **Q6. `--shared` із зайнятою орендою одразу падає**, без очікування. Одночасні деплої виключає concurrency-група в CI. Це вимога до деплою (§18).
7. **Q7. До `create_tenant_schema` і `drop_tenant_schema` мютекс не додаємо.** `pg_restore` у схему тенанта виконується лише в статусі MAINTENANCE.
8. **Q8. Вивід воркерів не буферизуємо**, але вмикаємо порядкову буферизацію (line buffering). На кожен тенант пишеться окремий лог-файл (§9.9).
9. **Новий статус `MAINTENANCE`**, яким володіє оператор (§2.3). Значення `choices` додаємо в **наявну** міграцію `tenants`, без нової міграції: продакшена немає.
10. **`lock_timeout` ставиться кодом мігратора** (`set_config` на з'єднанні тенанта). Ні `ALTER ROLE`, ні окремої ролі БД.
11. **Мютекс — оренда в рядку таблиці**, з фіксованим TTL 1 год без продовження і з командою примусового звільнення.
12. **B1, M1, M2, M4 з фінального рев'ю прийнято** (§5.4, §4.3, §4.2, §6.2).
13. **m6 = варіант (а).** Обробника `connection_created` немає. У runbook іде правило «міграції та `post_migrate`-обробники не закривають з'єднання з БД».
14. **Лог-файли:** лише налаштування `TENANT_MIGRATE_LOG_DIR`, без CLI-прапорця. Celery файлів не пише. Зберігаються дві попередні теки прогонів плюс поточна.
15. **`--summary-json=PATH`** входить у цю редакцію (2026-10-12): атомарний JSON-підсумок прогону з машинними кодами `reason` (§9.10).
16. **Стара команда зберігається як `migrate_schemas_legacy`** (2026-10-12): копія поточного `migrate_schemas.py` як запасний інструмент, не для деплою (§15.4).

---

## 2. Статусна модель та інваріанти

### 2.1 Статуси
`Tenant.Status`: `NEW`, `PENDING`, `ACTIVE`, `DEACTIVATED`, `FAILED`, **`MAINTENANCE`** («Maintenance (operator-owned)»).

Групи в коді мігратора:
- `PROVISIONABLE = ("new", "pending")`;
- `UPGRADABLE = ("active", "deactivated")`;
- `OPERATOR_OWNED = ("failed", "maintenance")`: мігратор не бере їх у прогін `--tenant`.

Тест звіряє групи з `Tenant.Status`.

### 2.2 Поведінка мігратора за статусом
| Статус на вході | Дія | Успіх | `Exception` | `BaseException` |
|---|---|---|---|---|
| NEW, схема є і непорожня, `previous_status ≠ failed` | огороджений CAS → FAILED `schema_exists` + forget | — | — | — |
| NEW / PENDING, код старіший за БД | без захоплення | skipped `code_older_than_db` | — | — |
| NEW / PENDING | оренда → CAS у PENDING (`previous_status='new'`) | CAS PENDING → ACTIVE + forget | обрив з'єднання → лишається PENDING; інакше → FAILED + forget | лишається PENDING; оренда за правилом §6.5 |
| ACTIVE / DEACTIVATED, план порожній, без targeted | без оренди | noop | — | — |
| ACTIVE / DEACTIVATED, схеми немає | оренда → огороджений CAS → FAILED `schema_missing` + forget | — | — | — |
| ACTIVE / DEACTIVATED | оренда → `run_migrations` | ok, статус без змін | failed, статус без змін | raise |
| MAINTENANCE без `--maintenance` | — | skipped `status_maintenance` | — | — |
| MAINTENANCE з `-s X --maintenance` | оренда → `run_migrations`; статус **не пишеться ніколи**, навіть якщо схеми немає | ok | failed | raise |
| FAILED | — | skipped `status_failed` | — | — |
| оренда зайнята | — | skipped `locked by <owner> since <since>` | — | — |
| огороджений CAS оновив 0 рядків | статус не пишемо | skipped `status_changed` / `lease_lost` / `tenant_deleted` | — | — |

### 2.3 MAINTENANCE
- **Вхід:** лише адмінка або скрипт відновлення, з ACTIVE, DEACTIVATED, FAILED. `previous_status` = статус до входу. Адмінка відмовляє, поки оренду тенанта тримає живий мігратор, і показує власника.
- **Вихід:** лише адмінка або скрипт. MAINTENANCE → ACTIVE (через стопер реактивації: схема має бути актуальною), → DEACTIVATED, → FAILED.
- **Мігратор і Celery:** не беруть таких тенантів у прогін. Деплой через них не червоніє, до `--max-failures` вони не рахуються. Мігрувати можна лише явно: `migrate_schemas -s X --maintenance`.
- **`reconcile --check`:** не перевіряє. `--report` показує.
- **Консоль (DRF API):** змінити не може; `DELETE` відмовляє (§14.3).
- **Веб:** 503 `tenant_maintenance` з `Retry-After`, але власна адмінка тенанта (`/admin/`) лишається доступною, як для FAILED.
- **Resolve-кеш:** `WARM_TTL_BY_STATUS["maintenance"] = 1800` (`tenants/resolver/config.py`), як для FAILED: обидва статуси операторські, а вихід з них — CAS адмінки з `forget_tenant` (§16). Погоджено 2026-10-12; тест `TtlForStatusTests` вимагає запис для кожного статусу.

### 2.4 Інваріанти
- **I1.** Тенанта мігрує лише власник його оренди. Кожен запис статусу мігратором — огороджений CAS (§4.4). Єдиний виняток — ручний запуск `migrate_schemas_legacy` (§15.4), який оренду не бере. Захисту від паралельної роботи з новим мігратором у коді немає: перед запуском обов'язкова ручна перевірка `migration_lease --list` (§19).
- **I2.** У PENDING переводить лише захоплення під орендою.
- **I3.** Кожен запис статусу — CAS. Кеш інвалідується тоді й лише тоді, коли CAS змінив рядок. Виняток: NEW → PENDING не інвалідується, бо обидва статуси дають 503.
- **I4.** PENDING → ACTIVE лише тоді, коли після прогону план порожній і прогін не targeted.
- **I5.** `reconcile_tenants` нічого не пише в БД.
- **I6.** Оновлення пише статус лише для `schema_missing` і лише для ACTIVE/DEACTIVATED.
- **I7.** MAINTENANCE змінюють лише адмінка і скрипт відновлення.
- **I8.** Після виходу з `migrate_tenant` жодне з'єднання процесу не має `lock_timeout` мігратора (§7).

---

## 3. Модулі та файли

| Файл | Зміст | ≈ рядків |
|---|---|---|
| `tenants/models.py` + **наявна** міграція `tenants` | `Status.MAINTENANCE`; модель `MigrationLease` | 25 |
| `tenants/migrator.py` (новий, єдиний модуль ядра) | `owner()`, `LEASE_TTL`; `lease_acquire/release/holder`, `shard_session_active`; `_fresh()`, `_open_shard()`, `_close_shard()`; `pending_plan()`, `schema_has_relations()`, `code_older_than_db()`, `check_reactivation()`; `is_conn_error()`, `is_lock_timeout()`; `invalid_indexes()`; `Result`, `RunOptions`; `migrate_tenant()`, `migrate_shared()`; `RunLog` (лог-файли) | ~340 |
| `tenants/management/commands/migrate_schemas.py` (переписати) | прапорці, етапи, вибір цілей, `pick_next`, fork-пул, `_worker_init`, сигнали, повторна перевірка `locked`, знімок після аварії, підсумок, `--summary-json`, коди виходу | ~290 |
| `tenants/management/commands/migration_lease.py` (новий) | `--list`, `--release`, `--force` | ~60 |
| `tenants/management/commands/migrate_schemas_legacy.py` (копія поточного `migrate_schemas.py`) | стара логіка без змін, змінено лише docstring і `help` (§15.4) | ~330 (копія) |
| `tenants/management/commands/reconcile_tenants.py` (переписати) | `--check`, `--report [-s] [--only-pending] [--plan]` | ~150 |
| `tenants/management/commands/migrate.py` + `tenants/test_runner.py` | заборона `migrate` у MT (§12) | ~30 |
| `commons/platform/migration_ops.py` | `LockTimeoutOff` / `LockTimeoutRestore` (§8.2) | ~30 |
| `tenants/checks/migrate.py` | 5 перевірок конфігурації (§13) | ~40 |
| Точкові правки | `tasks.py`, `console/views.py`, `console/admin.py`, `middleware.py`, `settings_*.py` | ~110 |
| CI-тест `test_migration_lint.py` | лінт нетранзакційних міграцій | ~40 |

**Налаштування:**
- `settings_base.py`: `TENANT_MIGRATE_DDL_LOCK_TIMEOUT = None`. Його читає лише `migration_ops`, бо бізнес-міграції виконуються і в standalone.
- `settings_multitenant.py`:
  - `TENANT_MIGRATE_DDL_LOCK_TIMEOUT = "5s"`;
  - `TENANT_MIGRATE_JOBS = 1`;
  - `TENANT_MIGRATE_JOBS_PER_SHARD = 1`;
  - `TENANT_MIGRATE_LOG_DIR = BASE_DIR / "logs" / "migrations"`.

  На проді значення перевизначаються в `settings_local_multitenant.py`.
- Константа в коді: `LEASE_TTL = timedelta(hours=1)`.

**Standalone.** Застосунок `tenants` підключено лише в `settings_multitenant.py:38`. Тому мігратор, оренда, команди і checks у standalone не існують, і гілки «standalone» в мігратора немає.

---

## 4. Оренда `MigrationLease`

### 4.1 Модель
```python
class MigrationLease(models.Model):
    key   = models.BigIntegerField(primary_key=True)   # tenant.pk; 0 = етап --shared
    owner = models.TextField(null=True)                # host:pid:start_utc; NULL = вільно
    since = models.DateTimeField(null=True)            # коли захоплено
    until = models.DateTimeField(null=True)            # since + LEASE_TTL
```
- Таблиця `tenants_migrationlease` лежить у `public` на `default`, поряд із `Tenant`. Тому огороджений CAS — це один `UPDATE`.
- FK на `Tenant` немає: ключ 0 не є тенантом, а видалення тенанта не повинно чіпати оренду.
- Рядок створюється при першому захопленні. Після видалення тенанта його рядок лишається і нічому не шкодить. `migration_lease --list` позначає такий рядок як «tenant deleted».
- Модель додаємо в наявну міграцію `tenants`, бо продакшена немає.

**Чим оренда не є:** журналом міграцій (це `django_migrations` у схемі тенанта), маркером помилки (після невдачі оренда звільняється) і статусом.

### 4.2 `owner()` (M2)
Формат: `host:pid:час_старту_процесу_UTC`, наприклад `app-1:4312:2026-10-10T12:00:03Z`.
```python
_owner_cache: tuple[int, str] | None = None

def owner() -> str:
    global _owner_cache
    pid = os.getpid()
    if _owner_cache is None or _owner_cache[0] != pid:      # новий процес після fork
        _owner_cache = (pid, f"{socket.gethostname()}:{pid}:{_process_start_utc()}")
    return _owner_cache[1]
```
- **Константи на рівні модуля немає.** Інакше воркери fork-пулу і дочірні процеси Celery prefork успадкували б значення батька, і огородження не розрізняло б процеси.
- `_process_start_utc()` бере справжній час старту процесу: поле 22 `/proc/self/stat`, `btime` з `/proc/stat` і `SC_CLK_TCK`. `psutil` у залежностях немає. Залежність від Linux прийнятна: на ній уже тримається `PR_SET_PDEATHSIG`.
- **Хто що порівнює:**
  - «оренда моя?» — лише сам процес, порівнянням свого збереженого рядка (`owner = :me`);
  - «робота ще йде?» (M1, `--release`) — за `application_name='tm:<pk>'`, рядок owner при цьому не розбирається;
  - людина звіряє хост, pid і час старту з `ps -o lstart= -p <pid>`, щоб відрізнити повторно виданий pid.

### 4.3 Захоплення (M1)

**Змінено після ревю (2026-10-12):** перевірка M1 (`shard_session_active`) тепер виконується перед **кожним** захопленням, а не лише для протухлої оренди, і саме захоплення — один `INSERT … ON CONFLICT DO UPDATE … WHERE owner IS NULL OR until < now()`. Причина: при обриві з'єднання мігратор повертає `conn_error` і за §6.5 **звільняє** оренду, а сервер може й далі виконувати міграцію (CIC, важкий `RunPython`); вільну оренду раніше брали без перевірки — та сама міграція могла піти вдруге паралельно. Нове повідомлення: `locked (lease free, backend still active: pid …)`. Ціна — один короткий запит до шарду лише для тенантів, яким треба мігрувати; з'єднання шарду після нього використовується далі. Опис нижче — первісний.

```text
lease_acquire(key, alias):
  row = SELECT owner, since, until FROM tenants_migrationlease WHERE key = :key
  якщо row.owner IS NOT NULL і row.until >= now()  → skipped "locked by <owner> since <since>"
  якщо row.owner IS NOT NULL і row.until <  now()  (протухла):
      якщо shard_session_active(alias, key)        → skipped "locked (lease expired, backend still active: pid …, running N min)"
      UPDATE … SET owner=:me, since=now(), until=now()+TTL
       WHERE key=:key AND owner=:old_owner AND until < now()      -- CAS за старим власником
      1 → наша; 0 → skipped "locked"
  інакше (рядка немає або owner IS NULL):
      INSERT INTO tenants_migrationlease(key, owner, since, until)
      VALUES (:key, :me, now(), now()+TTL)
      ON CONFLICT (key) DO UPDATE SET owner=EXCLUDED.owner, since=now(), until=EXCLUDED.until
       WHERE tenants_migrationlease.owner IS NULL
      RETURNING 1                                               -- 1 → наша; 0 → skipped "locked"
```
`shard_session_active(alias, key)` — спільний хелпер з `migration_lease --release`:
```sql
SELECT pid, now() - query_start AS running, left(query, 80)
  FROM pg_stat_activity
 WHERE datname = current_database()
   AND application_name = 'tm:<key>'
   AND state <> 'idle';
```
Для ключа 0 `alias = "default"`. Навіщо це: довга законна міграція (`CREATE INDEX CONCURRENTLY`, важка data-міграція) може пережити TTL. Без перевірки другий прогін перепланував би незаписану міграцію і виконав би її паралельно. Крім того, запит на сервері може тривати після SIGKILL клієнта. Тому TTL — запасна сітка, а не гарантія.

### 4.4 Огороджений CAS
```python
def _fenced_cas(pk, expected: tuple[str, ...], **fields) -> int:
    _fresh()
    return (Tenant.objects
            .filter(pk=pk, status__in=expected)
            .filter(Exists(MigrationLease.objects.filter(key=pk, owner=owner())))
            .update(status_changed_at=Now(), **fields))
```
- Огородження перевіряє `owner = me` **без** `until > now()`. Перехопити оренду можна лише через зміну `owner` (§4.3), тож `owner = me` достатньо. Водночас провізіонування, яке законно триває понад TTL, усе одно зможе записати ACTIVE.
- 1 рядок → `forget_tenant`. 0 рядків → один `SELECT` визначає причину (`status_changed`, `lease_lost`, `tenant_deleted`), статус не пишемо.

### 4.5 Звільнення
```sql
UPDATE tenants_migrationlease SET owner = NULL, since = NULL, until = NULL
 WHERE key = :key AND owner = :me;
```
Правило звільнення після `BaseException` наведено в §6.5.

---

## 5. Спільні частини ядра

### 5.1 `_fresh()`
```python
def _fresh() -> None:
    c = connections["default"]
    if not c.in_atomic_block:
        c.close()
```
Викликається на вході в `migrate_tenant` (m1) і перед **кожним** зверненням мігратора до `default`: оренда, CAS, читання тенанта. Від `CONN_MAX_AGE` і поведінки пулера не залежить. Всередині `atomic` (тести) нічого не робить.

### 5.2 `_open_shard(alias, key)` / `_close_shard(alias)`
```python
def _open_shard(alias, key):
    c = connections[alias]
    c.ensure_connection()
    with c.cursor() as cur:
        cur.execute("SELECT set_config('lock_timeout', %s, false), set_config('application_name', %s, false)",
                    [settings.TENANT_MIGRATE_DDL_LOCK_TIMEOUT, f"tm:{key}"])

def _close_shard(alias):
    c = connections[alias]
    if c.in_atomic_block:                       # лише в тестах (TestCase)
        with c.cursor() as cur:
            cur.execute("SET lock_timeout TO DEFAULT; SET application_name TO DEFAULT")
    else:
        c.close()
```
Як живе з'єднання і що це означає для Celery, описано в §7.

### 5.3 `Result` і `RunOptions`
```python
class Result(NamedTuple):
    kind: str                  # "ok" | "noop" | "failed" | "skipped"
    schema: str
    shard: str
    reason: str = ""           # машинний код (§9.10)
    msg: str = ""              # людський текст
    secs: float = 0.0
    invalid_indexes: tuple[str, ...] = ()
    conn_error: str | None = None      # None | "shard" | "default"   (m2, m3)

@dataclass(frozen=True)
class RunOptions:
    verbosity: int = 1
    fake: bool = False; fake_initial: bool = False; prune: bool = False; run_syncdb: bool = False
    app_label: str | None = None; migration_name: str | None = None
    maintenance: bool = False
    log_dir: Path | None = None        # None → лог-файли не пишуться (Celery)
    @property
    def targeted(self) -> bool: ...
```
`_upstream_options(opts, alias)` бере дефолти парсера upstream `migrate` один раз на процес і накладає: `database=alias`, `interactive=False`, `skip_checks=True`, `plan=False`, `check_unapplied=False` і targeted-поля.

**Правило вибору шарду.** `alias = t.shard.alias` визначається один раз на кроці 0 `migrate_tenant` (`t.shard.is_default` → failed `default_shard_anomaly`). Усі звернення до схеми тенанта йдуть через `connections[alias]`: `CREATE SCHEMA`, `pending_plan`, `schema_exists`, `schema_has_relations`, `invalid_indexes`, `shard_session_active`, `_open_shard`/`_close_shard`. Оренда й CAS статусу завжди йдуть на `default`.
- У upstream alias передається лише як `options["database"]`. `run_migrations` бере `connections[options.get('database', get_tenant_database_alias())]` і передає ті самі `options` у Django `migrate` (`django_tenants/migration_executors/base.py`). Без `database` усе пішло б на `default`.
- `_open_shard(alias, pk)` налаштовує той самий об'єкт `connections[alias]` цього процесу, який потім використовує `run_migrations`. Тому `lock_timeout` діє на DDL міграції.
- Роутер уже підтримує кілька баз: `TenantDatabaseRouter.allow_migrate` (`tenants/routers.py:172`). Поточний код робить так само: `tenants/management/commands/migrate_schemas.py:129-144`.
- Для `migrate_shared` alias — `"default"`, схема — `public`.
- Тест (`ProvisionTests`/`UpgradeTests`): тенант на `tenant_2` мігрує саме там, `CREATE SCHEMA` створює схему на `tenant_2`, а на `default` такої схеми немає.

### 5.4 `pending_plan(t)` (B1)
```python
MISSING = object()

def pending_plan(t) -> list[tuple[str, str]] | object:
    alias = t.shard.alias
    if not schema_exists(t.schema_name, alias):
        return MISSING
    c = connections[alias]
    try:
        c.set_schema(t.schema_name, include_public=False)   # django_migrations лише зі схеми тенанта
        ex = MigrationExecutor(c)
        return [(m.app_label, m.name) for m, back in ex.migration_plan(ex.loader.graph.leaf_nodes()) if not back]
    finally:
        _close_shard(alias)
```
**Чому саме так.** Без явної перевірки «схеми немає» не відрізнити від «схема відстає»: жодна відповідь `MigrationExecutor` не дає `MISSING`. PostgreSQL мовчки ігнорує неіснуючу схему в `search_path`, і результат залежить від того, як шукається `django_migrations`:
- **у нашому стеку (перевірено 2026-10-12):** інтроспекція django-tenants (`postgresql_backend/introspection.py`) на час `has_table()` ставить `search_path` лише на схему тенанта. Таблиці не знаходиться, «застосованого» немає, і відсутня схема дає **повний** план. Тест `PendingPlanTests` з вимкненою перевіркою падає саме так. Без перевірки мігратор вважав би тенант таким, що відстає, і пішов би в `run_migrations` замість Q1 (FAILED `schema_missing`), а `reconcile` і стопер показували б «відстає на N» замість «схеми немає»;
- **суміжна пастка, про яку попереджає upstream** (`django_tenants/migration_executors/base.py:47-49`): якщо `django_migrations` шукається через `search_path` разом із `public`, знаходиться `public.django_migrations`. Там записані всі застосунки, бо бізнес-застосунки є і в `SHARED_APPS`, тож план вийшов би порожнім, і тенант без схеми виглядав би актуальним.

Тому `schema_exists` іде першою і дає `MISSING` незалежно від поведінки інтроспекції. `include_public=False` закриває суміжний випадок: схема є, але порожня. Тоді відповідь — «нічого не застосовано».

**Хто викликає:**
- мігратор (крок NOOP і оновлення);
- `reconcile --check/--report/--plan`;
- стопер реактивації (DEACTIVATED → ACTIVE, MAINTENANCE → ACTIVE).

### 5.5 Інші функції
- **`schema_has_relations(alias, schema)`:** `SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s)`.
- **`code_older_than_db()`:** чи є в `public.django_migrations` міграції, яких цей код не знає (тобто БД уже змігрував новіший реліз):
  ```python
  def code_older_than_db() -> bool:
      loader = MigrationLoader(None, ignore_no_migrations=True)
      known = set(loader.disk_migrations)
      for m in loader.disk_migrations.values():
          known.update(m.replaces or ())                  # squash: замінені імена теж «відомі»
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
  ```
  - **Евристика** «невідомі міграції застосовано пізніше за останню відому»: видалена з коду стара міграція, що лишилась у таблиці, має раніший `applied` і не блокує. Без евристики вона назавжди блокувала б провізіонування.
  - Обмеження: `applied` — це `timezone.now()` клієнта, тож розбіжність годинників має бути набагато меншою за інтервал між релізами.
  - Лише застосунки з `loader.migrated_apps`: записи сторонніх застосунків, яких немає в `INSTALLED_APPS`, ігноруються.
  - Недоступний `default` → виняток пробрасується (мігратор: failed `conn_error="default"`; стопер: `ShardUnavailable` → 503).
  - `set_schema_to_public()` — метод бекенда django-tenants (`django_tenants/postgresql_backend/base.py:116`).
- **`check_reactivation(t)`:**

  | Умова | Виняток |
  |---|---|
  | `code_older_than_db()` | `DeployInProgress` |
  | план `MISSING` | `SchemaMissing` |
  | план непорожній | `SchemaBehind(n)` |
  | шард недоступний | `ShardUnavailable` |

  **Реалізовано (крок 7, 2026-10-12):** усі винятки мають спільну базу `ReactivationBlocked` з атрибутами `code` (машинний код консолі) і `status` (409/503), тож адмінка й консоль ловлять один клас. Замість `ShardUnavailable` піднімається `ReactivationUnavailable` (`code="shard_unreachable"`, 503) з `ShardUnavailable` як причиною: сам `ShardUnavailable` лишився внутрішнім сигналом `lease_acquire`. Недоступний `default` (у `code_older_than_db`) дає той самий виняток. `SchemaBehind.pending` — імена міграцій `app.name`.
- **`is_conn_error(e)`:** проходить ланцюжок `__cause__`/`__context__`. Це `OperationalError`/`InterfaceError` з sqlstate `None`, клас `08*`, `57P01`–`57P03`. **`is_lock_timeout(e)`:** sqlstate `55P03`.
  **Змінено після ревю (2026-10-13):** додано `57P05` (`idle_session_timeout`) і `25P03` (`idle_in_transaction_session_timeout`) — сервер сам закриває сесію, тож провізіонування лишається PENDING (відновлюване), а не FAILED. Код із цього списку або клас `08*` рахується обривом за **власним** sqlstate помилки, хоч би якого вона класу: `25P03` у psycopg — `InternalError`, не `OperationalError`. Перевірено на сервері: `53300 too many connections` при **підключенні** psycopg 3 віддає без sqlstate (`None`) — це вже обрив за правилом `None`, знахідку ревю про 53300 знято. `40P01`, `40001`, `57014`, `53100`/`53200` лишаються `migration_error` (тести `ClassifyTests`, `ServerErrorClassificationTests`).
- **`invalid_indexes(alias, schema)`:** окремий короткий запит на шарді (після `_close_shard`, щоб не залежати від стану з'єднання міграції):
  ```sql
  SELECT c.relname FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE NOT i.indisvalid AND n.nspname = %s ORDER BY 1
  ```
  Живий `CREATE INDEX CONCURRENTLY` теж дає `indisvalid = false`, тому `reconcile --report` показує поруч стан оренди й активної сесії `tm:<pk>`. Помилка самого запиту не змінює `Result` (індекси просто не показуються).

---

## 6. Алгоритм на тенант: `migrate_tenant(pk, opts) -> Result`

### 6.1 Каркас
```text
0. _fresh(); t = Tenant(pk).select_related("shard")
   DoesNotExist              → skipped "tenant_deleted"
   default недоступний       → failed, conn_error="default"
   t.shard.is_default        → failed "default_shard_anomaly"
1. t.status == FAILED                              → skipped "status_failed: reset via admin FAILED→NEW"
   t.status == MAINTENANCE and not opts.maintenance → skipped "status_maintenance"
2. t.status in UPGRADABLE (або MAINTENANCE з прапорцем) і не targeted:
       plan = pending_plan(t); plan == []          → noop (без оренди і без записів у default)
3. lease_acquire(pk, alias) не вдалося             → skipped "locked …"
4. try:
       _fresh(); перечитати статус (умови кроку 1 ще раз)
       PROVISIONABLE → _provision;  UPGRADABLE / MAINTENANCE+прапорець → _upgrade;  інше → skipped "status_changed"
   finally:
       idle = (shard.connection is None) or (pgconn.transaction_status != ACTIVE)
       _close_shard(alias)                         ← спершу закрити шард (m5)
       якщо вихід звичайний або idle: lease_release(pk)
5. Лог tenants.migrate: extra={schema, shard, kind, msg, secs}; failed → ERROR, skipped → WARNING, інше → INFO.
```
Якщо `opts.log_dir` задано, кроки 2–4 виконуються під tee у файл тенанта (§9.9).

### 6.2 `_provision`
1. `opts.targeted` → skipped (і так відсічено валідацією в `handle()`).
2. `code_older_than_db()` → skipped `code_older_than_db`.
3. **M4.** Якщо статус NEW, `previous_status ≠ failed` і `schema_has_relations()`: огороджений CAS `new → failed`, `last_error="schema_exists: …"` → forget → failed `schema_exists`. Порожня схема (наприклад, створена `create_tenant_schema`) проходить далі. Продовження з PENDING цю перевірку не проходить. Легітимне прийняття наявної схеми: адмінка FAILED → NEW з підтвердженням (§14.2), після чого `previous_status = failed`.
4. **Захоплення:** огороджений CAS `new|pending → pending`, `previous_status='new'`, `last_error=''`. 0 рядків → skipped. Якщо статус був PENDING, у лог іде `RESUME <schema>`. Кеш не інвалідуємо.
5. `CREATE SCHEMA IF NOT EXISTS <quote_schema>` **на `connections[alias]`** (не на `default`) → `_open_shard(alias, pk)` → `run_migrations(args, _upstream_options(opts, alias), "standard", schema)`.
6. `pending_plan(t)` непорожній → `raise Incomplete(n)` (I4).
7. **Фіналізація:** огороджений CAS `pending → active`, `previous_status='pending'`, `last_error=''` → forget → ok (або ok `RESUMED`).
   **Додано після ревю (2026-10-13):** безпосередньо перед цим CAS — повторний `code_older_than_db()`. Інакше старий Celery-воркер, що захопив тенанта до викатки й доробив свій (старий) план уже після `--shared` нового релізу, зробив би його ACTIVE на старій схемі, поки новий код його обслуговує (повторна перевірка `locked` і `--check` вважають PENDING з живою орендою нормою). Тепер → skipped `code_older_than_db`, статус лишається PENDING, оренду звільнено; Celery retry або наступний `migrate_schemas` доробляє вже новим кодом (`ProvisionTests.test_newer_release_during_provisioning_leaves_pending`).
8. `except Exception as e`:
   - `is_conn_error(e)`: статус лишається PENDING (провізіонування можна продовжити) → failed з `conn_error="shard"` або `"default"`;
   - інакше: огороджений CAS `pending → failed`, `last_error` = хвіст traceback (4000 символів) → forget → failed (`lock_timeout` / `incomplete_after_run` / `migration_error`) + `invalid_indexes`.
9. `except BaseException`: статус не пишемо (лишається PENDING), raise.

### 6.3 `_upgrade`
1. План `MISSING` (схеми немає):
   - ACTIVE/DEACTIVATED: огороджений CAS `status → failed`, `previous_status=<status>`, `last_error='schema "<s>" missing on <alias> during upgrade'` → forget → failed `schema_missing`;
   - MAINTENANCE: failed `schema_missing` **без** запису статусу.
2. `_open_shard(alias, pk)` → `run_migrations(...)` → ok. Для targeted → ok `targeted, remaining=<n>`.
3. `except Exception` → failed (`lock_timeout` / `migration_error` / `conn_error`) + `invalid_indexes`. Статус не змінюється.
4. `except BaseException` → raise.

### 6.4 `migrate_shared(opts)`
1. `to_regclass('tenants_migrationlease') IS NULL` (свіжа БД, перший `--shared`) → виконуємо без оренди. Це постійна гілка для першого розгортання, а не перехідний shim.
2. Інакше `lease_acquire(0, "default")`. Якщо зайнято → failed `shared stage busy: held by <owner> since <since>`, одразу, без очікування (Q6).
3. `_open_shard("default", 0)` → `run_migrations(..., schema_name="public")` → ok. `Exception` → failed.
4. `finally`: `_close_shard("default")`, звільнення за правилом §6.5. Лог — `_shared.log`.

### 6.5 Звільнення оренди після переривання
- **Успіх або `Exception`:** оренду звільняємо завжди.
- **`BaseException` (KeyboardInterrupt у послідовному режимі):** спершу запам'ятовуємо `idle`, потім закриваємо шард, і **лише якщо `idle`** звільняємо оренду. `idle` означає, що з'єднання немає або `transaction_status ≠ ACTIVE`: psycopg дочекався кінця запиту на сервері (`psycopg/connection.py:451-463`). Інакше оренда лишається, а в підсумку друкується підказка `migration_lease --release <schema>`.
- **SIGKILL:** `finally` не виконується. Оренда живе до TTL або до `--release`, і M1 не дасть перехопити її, поки бекенд активний.

---

## 7. Життєвий цикл з'єднань і Celery (обов'язковий підрозділ)

**Правило.** `finally` у `migrate_tenant` і `migrate_shared` **завжди** закриває з'єднання шарду (`_close_shard`). Всередині чужого `atomic`, тобто лише в тестах, замість закриття виконується `SET lock_timeout TO DEFAULT; SET application_name TO DEFAULT`.

**Хронологія одного тенанта:**
```text
_open_shard(alias)       → з'єднання (нове або наявне) + set_config lock_timeout=5s, application_name=tm:<pk>
run_migrations(...)      → усі DDL цього тенанта йдуть з таймаутом
finally: _close_shard    → TCP закрито, серверна сесія завершилась, lock_timeout зник разом із нею
наступний запит процесу  → Django відкриває нове з'єднання з чистою сесією
```

- **Чому не можна покладатися на upstream.** `run_migrations` закриває з'єднання сам (`commit` → `close` → `connection=None`, `django_tenants/migration_executors/base.py:63-66`), але **лише після успіху**. На помилці з'єднання лишається відкритим. Усередині `atomic` upstream його теж не закриває (`base.py:67-72`). Наш `close()` після закриття upstream нічого не робить.
- **Додано після ревю (2026-10-13): `RESET ALL` перед `close()`** (`_reset_session`, лише для простою сесії; відкрита транзакція спершу відкочується). Закриття клієнта має завершити серверну сесію — але через RDS Proxy з'єднання БД відключеного клієнта «повертається в пул», а доля «запіненого» не задокументована (V1). Скидання гарантує, що з пулу не прийде бекенд з `lock_timeout=5s` чи `application_name=tm:<key>` (останнє змусило б M1 вважати тенанта зайнятим). Сесію, що ще виконує запит (після другого Ctrl-C), не чіпаємо. Перевірити на staging з проксі лишається потрібним.
- **Чому це критично для Celery.** Celery-воркер живе довго. Після `provision_tenant` той самий процес виконує інші задачі через той самий `connections[alias]`. Якби на з'єднанні лишився `lock_timeout=5s`, звичайні задачі падали б з 55P03 без видимої причини.
- **`default`** закривається за тим самим принципом у `_fresh()` перед кожним зверненням мігратора.
- **Обов'язковий тест** (`CeleryConnectionHygieneTests`): в одному процесі запустити `provision_tenant` з успіхом і окремо з помилкою. Після цього на тому самому alias `SHOW lock_timeout` має дорівнювати серверному дефолту, а `application_name` не повинен починатися з `tm:`. Те саме перевірити для `default` і `--shared`.
- **Правило для авторів міграцій (m6 = а):** міграції та `post_migrate`-обробники **не закривають і не перевідкривають** з'єднання з БД. Нове з'єднання працювало б без `lock_timeout`.

---

## 8. `lock_timeout` і нетранзакційні міграції

### 8.1 Два рівні захисту
| | Оренда | `lock_timeout` |
|---|---|---|
| Від кого захищає | від іншого мігратора | від трафіку застосунку (веб, Celery, звіти) |
| Де працює | наш код, таблиця `MigrationLease` | PostgreSQL, кожна DDL-команда |
| Чекає? | ні, відповідь миттєва | так, до 5 с |
| Якщо не вдалося | skipped `locked`, перехід до наступного тенанта | 55P03 → транзакцію міграції відкочено → failed `lock_timeout` |

- **Хто ставить:** лише `_open_shard` у процесі мігратора, на кожен тенант, після захоплення оренди і перед `run_migrations`. Веб, Celery-задачі, DBA і ролі БД його не задають.
- **55P03 при оновленні:** статус не змінюється, наступний прогін повторить саме цю міграцію.
- **55P03 при провізіонуванні:** FAILED (Q3). Практично цього не буває: нова схема порожня. Якщо 55P03 таки з'явиться, це сигнал, що порушено правило «без міграцій, що чіпають `public`».

### 8.2 `commons/platform/migration_ops.py`
```python
from django.conf import settings
from django.db import migrations

class LockTimeoutOff(migrations.RunSQL):
    def __init__(self):
        super().__init__(sql="SET lock_timeout = 0", reverse_sql=migrations.RunSQL.noop)

def _restore(apps, schema_editor):
    v = getattr(settings, "TENANT_MIGRATE_DDL_LOCK_TIMEOUT", None)
    with schema_editor.connection.cursor() as c:
        if v is None:
            c.execute("SET lock_timeout TO DEFAULT")
        else:
            c.execute("SELECT set_config('lock_timeout', %s, false)", [v])

class LockTimeoutRestore(migrations.RunPython):
    def __init__(self):
        super().__init__(_restore, migrations.RunPython.noop)
```
- `deconstruct()` обох класів повертає `commons.platform.migration_ops.<Class>` без аргументів (перевизначити `deconstruct`, бо батьківський віддає `sql`/`code`).
- Модуль не залежить від `tenants`: імпортується й працює і в standalone (там значення `None` → `TO DEFAULT`).

Рецепт для `atomic = False`:
```python
operations = [
    LockTimeoutOff(),
    migrations.SeparateDatabaseAndState(
        state_operations=[migrations.AddIndex("model", models.Index(fields=["f"], name="app_model_f_0042_idx"))],
        database_operations=[
            migrations.RunSQL('DROP INDEX CONCURRENTLY IF EXISTS "app_model_f_0042_idx"', migrations.RunSQL.noop),
            migrations.RunSQL('CREATE INDEX CONCURRENTLY "app_model_f_0042_idx" ON "app_model" ("f")',
                              'DROP INDEX CONCURRENTLY IF EXISTS "app_model_f_0042_idx"'),
        ]),
    LockTimeoutRestore(),
]
```
Правила:
- кожна міграція має унікальне ім'я індексу;
- `CONCURRENTLY IF NOT EXISTS` заборонено;
- повтор після INVALID безпечний завдяки `DROP … IF EXISTS`;
- рецепт працює в обох режимах (MT і standalone).

---

## 9. `migrate_schemas`

### 9.1 Прапорці
- **Upstream:** `--shared`, `--tenant`, `-s/--schema_name`, `--database`, `--noinput`, `app_label`, `migration_name`, `--fake`, `--fake-initial`, `--prune`, `--run-syncdb`.
- **Наші:**
  - `--jobs`, `--jobs-per-shard` (дефолти з налаштувань; валідуються в `handle()`, `per_shard ≤ jobs`);
  - `--keep-going`;
  - `--max-failures=N` (типово 3, N ≥ 1);
  - `--maintenance` (лише разом з `-s`);
  - `--summary-json=PATH`: машиночитний підсумок прогону (§9.10).
- **Відхиляються з підказкою:**
  - `--check`, `--list` → `reconcile_tenants --check`;
  - `--plan` → `reconcile_tenants --report --plan`;
  - `--executor` ≠ `standard`.
- **Додано після ревю (2026-10-13):** відмова з підказкою для `-s public --tenant` (раніше `--tenant` мовчки відкидався) і для `--tenant --database X`, де X — шард `default`, неактивний шард або alias без запису `Shard` (раніше «No tenants to migrate», код 0). `--max-failures` без `--keep-going` — попередження «ignored» (дефолт 3 тепер розв'язується в `_validate`, щоб явне значення можна було відрізнити). Повідомлення `--jobs-per-shard > --jobs` пояснює, що `--jobs` узято з `TENANT_MIGRATE_JOBS`.
- **Targeted-прапорці** (`--fake`, `--fake-initial`, `--prune`, `--run-syncdb`, `app_label`, `migration_name`) дозволені лише з однією ціллю: `-s X`, де X — ACTIVE, DEACTIVATED або MAINTENANCE з `--maintenance`, або `--shared`. Для них вимкнено NOOP-шорткат і повторну перевірку `locked`.

### 9.2 Етапи
Без `--shared`/`--tenant` виконуються обидва етапи послідовно. Невдача `--shared` (будь-яка, включно з «busy») зупиняє прогін до етапу тенантів.

### 9.3 Вибір цілей
- **`--tenant`:** статус ∈ PROVISIONABLE ∪ UPGRADABLE, лише на шардах з `is_default=False` і `is_active=True` (як у старій команді; погоджено 2026-10-12), без `public`, з опційним `--database`. Порядок: `get_tenant_migration_order()`, інакше `pk`. Цілі групуються за шардами. FAILED і MAINTENANCE не беруться.
- **`-s X`:** один тенант з будь-яким статусом. FAILED → skipped. MAINTENANCE без `--maintenance` → skipped.

### 9.4 Планування і `--max-failures`
- `pick_next(queues, inflight, jobs, per_shard)` — чиста функція: шарди по колу, FIFO всередині шарду.
- `consecutive[shard]` рахується **в порядку завершення** (Q4): failed збільшує, ok/noop скидає, skipped не змінює.
- Без `--keep-going` перша невдача зупиняє прогін, якщо ще лишились цілі. З `--keep-going` значення `≥ N` на будь-якому шарді дає глобальний `Stop: max_failures:<shard>`.
- `conn_error == "shard"` → решта черги шарду отримує `not run (shard unreachable)` і до лічильника не йде. Інші шарди продовжують.
- `conn_error == "default"` → глобальна зупинка.
- **Зупинка:** нових цілей не беремо, in-flight доробляються, решта отримує `not run`.

### 9.5 Повторна перевірка `locked` наприкінці
Для кожного skipped `locked` статус перечитується:
- UPGRADABLE і план порожній → ok `done by another run`;
- PENDING і живий власник оренди → ok `provisioning in progress elsewhere` (m4: Celery-провізіонування під час деплою не робить його червоним);
- ACTIVE, а до того був NEW/PENDING → ok;
- інакше лишається skipped.

### 9.6 Паралельність
- Паралельний режим вмикається лише тоді, коли команду викликано з CLI (`run_from_argv`), `jobs > 1` і цілей більше однієї. В усіх інших випадках, зокрема для `call_command`, Celery і `bootstrap_tenant`, режим послідовний.
- Перед створенням пулу: `connections.close_all()`. Далі `ProcessPoolExecutor(jobs, mp_context=get_context("fork"), initializer=_worker_init, initargs=(os.getpid(),))`.
- `_worker_init(parent_pid)`:
  - SIGINT і SIGTERM → `SIG_IGN`;
  - `prctl(PR_SET_PDEATHSIG, SIGKILL)` через `ctypes`; `if os.getppid() != parent_pid: os._exit(1)`;
  - `sys.stdout.reconfigure(line_buffering=True)`; те саме для `stderr`.
- **Уточнення реалізації (крок 4, 2026-10-12):**
  - воркерів `min(jobs, кількість цілей)`; батько на час пулу теж переходить на line buffering (його рядки результатів теж цілі) і після пулу повертає попередній режим;
  - поки пул живий, батько не звертається до БД; знімок (§9.7) читається вже після закриття пулу;
  - виняток, який `migrate_tenant` підняв у воркері, дає цій цілі failed `migration_error` і йде в лічильники §9.4. Послідовний режим у такому разі перериває прогін, але пул так не може: інші тенанти ще в польоті;
  - `call_command` з `--jobs>1` виконується послідовно з повідомленням «parallel mode runs only from the command line».

### 9.7 Сигнали
Лише CLI і головний потік. Попередні обробники відновлюються в `finally`.
- **Перший SIGINT/SIGTERM:** дренаж (§9.4, «Зупинка»).
- **Другий у паралельному режимі:** SIGKILL для `multiprocessing.active_children()` (m7). Оренди не звільняємо.
- **Другий у послідовному режимі:** `raise KeyboardInterrupt`, далі правило §6.5.
- **Після жорсткої зупинки або `BrokenProcessPool`:** для кожного in-flight тенанта друкуємо знімок з БД: статус, скільки міграцій відстає (або «схеми немає»), owner оренди, підказку `migration_lease --release <schema>`.
- **Уточнення реалізації (крок 4, 2026-10-12):**
  - обробник лише ставить прапорці, вбиває воркерів і пише в fd 2 через `os.write`; черги чіпає основний цикл (паралельний опитує прапорець кожні 0,2 с). Інакше обробник міг би перервати `print` чи операцію над `deque` посеред виконання;
  - перший сигнал під час `--shared` дає змогу `--shared` доробити, а етап тенантів уже не починається (`Stop: signal`);
  - жорстка зупинка з CLI завершується кодом 1, як і будь-який зупинений прогін (§9.8). Чужий Ctrl-C під `call_command` (обробників там немає) і далі пробрасує `KeyboardInterrupt` (код 130 у JSON);
  - `BrokenProcessPool` без нашого сигналу (воркер убив OOM killer) дає `Stop: broken_pool`. Решту воркерів батько добиває SIGKILL: upstream шле їм SIGTERM, а вони його ігнорують. Результати, що встигли повернутися, зараховуються;
  - для `--shared` знімок містить лише owner оренди 0, а підказка — `migration_lease --release shared`.

### 9.8 Підсумок і коди виходу
- **Підсумок:** рядок на кожну ціль (`kind`, `schema`, `shard`, секунди, `msg`, INVALID-індекси), рядок `Stop: <причина>` (якщо прогін зупинено), підсумкові лічильники і `Logs: <тека прогону>`.
- **Код 0:** усі цілі ok/noop (з урахуванням §9.5) і прогін не зупинявся.
- **Код 1:** усе інше. Під `call_command` ненульовий код повертається як `CommandError`.
- **Помилки розбору аргументів** дають код 2 (поведінка argparse/Django); пайплайн реагує на нього як на будь-який ненульовий.

### 9.9 Лог-файли тенантів
- **Структура:**
  ```text
  <TENANT_MIGRATE_LOG_DIR>/
    2026-10-10T12-00-03Z_app-1_4312/      ← один прогін: час старту UTC, хост, pid
      _shared.log                         ← етап --shared
      acme.log                            ← тенант acme
      beta.log
      _summary.log                        ← той самий підсумок, що й на екрані
  ```
- **Вміст файлу тенанта:**
  - шапка (schema, shard, статус на старті, owner оренди, час початку);
  - **повний** вивід `run_migrations` без префікса;
  - повний traceback при помилці;
  - фінальний рядок: результат, тривалість, INVALID-індекси.
- **Механізм:** на час `migrate_tenant` stdout/stderr підміняються tee-обгорткою, яка пише кожен рядок і в живий stdout, і у файл, з flush після кожного рядка. Воркер мігрує один тенант за раз, тож у кожен файл пише рівно один процес. Файл переживає SIGKILL у тому обсязі, який встиг записатися.
- **Хто пише:** лише `migrate_schemas`, запущений **з CLI** (`run_from_argv`; рішення 2026-10-12). Паралельний режим (§9.6) теж лише з CLI, тож він завжди пише файли. Під `call_command` (Celery до кроку 7, `bootstrap_tenant`, тести) `log_dir=None`: інакше ротація з таких викликів могла б видалити теку деплою, що ще триває, а в контейнері воркера тека може бути недоступна на запис. Celery-провізіонування файлів не пише: там важливий кінцевий результат (`Result` + запис `tenants.migrate`), а з тенантом, що впав, розбираються окремо.
- **Зберігання:** на старті `migrate_schemas` лишаються дві найновіші попередні теки, старіші видаляються, потім створюється тека поточного прогону.
- **Розташування:** лише налаштування `TENANT_MIGRATE_LOG_DIR`, без CLI-прапорця.
- **Імена (змінено після ревю, 2026-10-13):** файли прогону починаються з `_` (`_shared.log`, `_summary.log`; раніше `summary.log` перезаписував лог тенанта зі схемою `summary`). `SCHEMA_NAME_RE` вимагає першим символом `[a-z0-9]`, тож тенант із таким ім'ям неможливий. Ім'я схеми, що не проходить `SCHEMA_NAME_RE` (legacy/обхідний рядок: модельне поле приймає і `/`, і `..`), дає `_tenant_<pk>.log` — без конфлікту й без виходу за теку прогону; справжнє ім'я — у шапці файлу. Функція `migrator.tenant_log_name()`.

### 9.10 `--summary-json=PATH`
Додано й погоджено 2026-10-12 разом з усіма деталями дизайну нижче (раніше було у §23 «Відкладено»). Той самий підсумок, що й §9.8, але для CI і скриптів: без розбору тексту.

- **Куди:** лише у файл `PATH`. Значення `-` (stdout) заборонене: stdout зайнятий виводом міграцій. Батьківська тека має існувати й бути доступною на запис. Це перевіряється в `handle()` до початку прогону, інакше `CommandError` (код 1).
- **Коли пишеться:** один раз наприкінці прогону, у `finally` команди. Отже, і при коді 1, і після дренажу, і після жорсткої зупинки чи `BrokenProcessPool` (тоді зі знімком з §9.7). Не пишеться при помилці розбору аргументів (код 2) і при SIGKILL батька.
- **Порядок (змінено після ревю, 2026-10-13):** спершу JSON, потім `_summary.log`, екран — останнім. Вивід на екран і в tee ковтає `BrokenPipeError`/`OSError`: Ctrl-C отримує вся група процесів і вбиває читача `| tee`; раніше наступний рядок валив міграції в польоті (дренаж ставав failed), а впалий друк підсумку губив JSON (`DeadStdoutTests`, `test_dead_live_stream_never_breaks_the_tee`).
- **Атомарність:** запис у `PATH.tmp` у тій самій теці, потім `os.replace`. CI ніколи не бачить половину файлу. Якщо файл лишився від попереднього прогону, він перезаписується.
- **Хто пише:** лише `migrate_schemas`. Celery, `bootstrap_tenant` і `call_command` без прапорця його не пишуть.
- **Формат (`version: 1`):**
  ```json
  {
    "version": 1,
    "started_at": "2026-10-12T12:00:03Z",
    "finished_at": "2026-10-12T12:04:41Z",
    "host": "app-1", "pid": 4312,
    "argv": ["migrate_schemas", "--tenant", "--jobs=4", "--keep-going"],
    "options": {"stages": ["tenant"], "jobs": 4, "jobs_per_shard": 1,
                "keep_going": true, "max_failures": 3, "maintenance": false, "targeted": false},
    "log_dir": "/srv/app/logs/migrations/2026-10-12T12-00-03Z_app-1_4312",
    "exit_code": 1,
    "stop": {"reason": "max_failures", "shard": "tenant_1"},
    "counts": {"ok": 40, "noop": 112, "failed": 3, "skipped": 1, "not_run": 20, "interrupted": 0},
    "results": [
      {"stage": "tenant", "schema": "acme", "tenant_id": 17, "shard": "tenant_1",
       "status_before": "active", "kind": "failed", "reason": "lock_timeout",
       "msg": "canceling statement due to lock timeout", "secs": 5.1,
       "invalid_indexes": [], "conn_error": null, "rechecked": false, "log_file": "acme.log"}
    ]
  }
  ```
- **Поля:**
  - `stop`: `null`, якщо прогін не зупинявся. Інакше `reason` ∈ `fail_fast`, `max_failures` (+`shard`), `default_unreachable`, `shared_failed`, `signal`, `hard_stop`, `broken_pool`.
  - `results`: рядок на кожну ціль у порядку вибору цілей (§9.3), етап `shared` першим. `kind` ∈ `ok`, `noop`, `failed`, `skipped`, `not_run` (ціль не запускалась через зупинку або недоступний шард), `interrupted` (in-flight при жорсткій зупинці; тоді додаються `snapshot: {status, behind, lease_owner}` з §9.7). У `snapshot`: `behind` = кількість незастосованих міграцій або `null`, якщо схеми немає (і завжди `null` для `public`); `status` = `"deleted"`, якщо тенанта видалено; якщо стан прочитати не вдалося, додається ключ `error`. Для `interrupted` `reason` = `hard_stop` в обох випадках (жорстка зупинка і `broken_pool`), відрізняє їх `stop.reason`.
  - `reason`: короткий машинний код із фіксованого списку (див. нижче). `msg` — людський текст, на нього скриптам спиратися не можна.
  - `rechecked: true` — результат змінено повторною перевіркою `locked` (§9.5).
  - `log_file`: ім'я файлу відносно `log_dir`.
  - `counts` рахуються після §9.5. `exit_code` завжди збігається з кодом процесу.
- **Коди `reason`** (константи в `tenants/migrator.py`, тест звіряє список):
  `applied`, `resumed`, `targeted`, `up_to_date`, `done_by_another_run`, `provisioning_elsewhere`, `locked`, `status_failed`, `status_maintenance`, `status_changed`, `lease_lost`, `tenant_deleted`, `code_older_than_db`, `schema_missing`, `schema_exists`, `lock_timeout`, `migration_error`, `incomplete_after_run`, `conn_error`, `default_shard_anomaly`, `shard_unreachable`, `stopped`, `hard_stop`.
- **Наслідок для `Result` (§5.3):** нове поле `reason: str`. Його заповнює код, який зараз формує `msg`. Текстовий підсумок не змінюється.
- **Сумісність:** нові поля додаються без зміни `version`. Перейменування чи видалення поля → `version: 2`.

---

## 10. `migration_lease`

| Команда | Дія |
|---|---|
| `--list` | key, schema (або `shared`, або **`tenant deleted`**), owner, since, until, `EXPIRED`, наявність активної сесії `tm:<key>` |
| `--release <schema\|shared>` | `shard_session_active()`: якщо активна сесія є, відмова з pid, тривалістю і запитом; інакше `UPDATE … SET owner=NULL …` для ключа, з виводом того, що звільнено |
| `--release <schema\|shared> --force` | звільнити без перевірки (у runbook — лише коли процес гарантовано мертвий і бекенда немає) |

**Змінено після ревю (2026-10-12):** `--release` без `--force` відмовляє і для **непротухлої** оренди (повідомлення «live until …»): живий власник між двома запитами для `pg_stat_activity` — `idle`, і перевірка сесії його не бачить. Звичайний `--release` — для протухлих оренд; `--force` — коли оператор перевірив, що процес-власник мертвий.

**Уточнення реалізації (крок 5, 2026-10-12):**
- Ціль `--release`: ім'я схеми, `shared` (ключ 0) або `key=<n>`. Останнє потрібне для оренди видаленого тенанта: схеми вже немає, а ім'я схеми може складатися лише з цифр, тож «голе» число двозначне. `--list` друкує саме цю форму. Якщо тенант зі схемою `shared` існує, `--release shared` відмовляє з підказкою `key=0` / `key=<pk>`.
- Звільнення огороджене власником, якого показали оператору (`UPDATE … WHERE key=%s AND owner=%s`, `migrator.lease_force_release`). Якщо між перевіркою і записом оренду встиг узяти новий прогін, її не чіпаємо (код 1, «changed hands»). Кожне звільнення пишеться в лог `tenants.migrate` (WARNING).
- Для оренди видаленого тенанта шард невідомий, тому активну сесію `tm:<key>` шукаємо на всіх недефолтних шардах. Недоступний шард без `--force` → відмова.
- Живу (не протухлу) оренду без активної сесії `--release` звільняє, як і написано вище. Окремої перевірки «процес-власник живий» немає.

---

## 11. `reconcile_tenants` (тільки читання)

- **`--check`** (гейт деплою):
  - перевіряє `public` і всі ACTIVE;
  - відставання або `MISSING` → 1;
  - `code_older_than_db()` → 1 з поясненням;
  - недоступний `default` або шард з ACTIVE-тенантами → 1;
  - застряглий PENDING (оренда вільна або протухла без активної сесії) → `WARN stuck PENDING`, на код не впливає;
  - DEACTIVATED, NEW, FAILED, MAINTENANCE не перевіряються;
  - підсумок `checked_active=N behind=B missing=M pending_stuck=P unreachable_shards=[…]`.
- **`--report [-s X] [--only-pending] [--plan]`.** На кожен тенант:
  - статус;
  - відставання: n або «схеми немає»;
  - оренда: owner, since, expired;
  - PENDING «у роботі» чи «застряг»;
  - INVALID-індекси;
  - підказка з наступною командою: `migrate_schemas -s X`, `migration_lease --release X` або «адмінка FAILED → NEW».

  `--plan` додає імена незастосованих міграцій. Перший рядок звіту — `public`.
- **Що видаляємо:** `_apply_status`, `_eval_*` з автовиправленнями, `--dry-run`, `_applied`/`_expected`/`_tenant_app_labels` (`reconcile_tenants.py:123-263`). Docstring переписуємо.
- **Уточнення реалізації (крок 5, 2026-10-12):** команду переписано повністю, старих методів немає (це й частина «видалити автовиправлення reconcile» з кроку 7).
  - Потрібен рівно один із режимів `--check` / `--report`; `-s`, `--only-pending`, `--plan` дозволені лише з `--report`. `--report` завжди завершується кодом 0.
  - «Відставання» = `migrator.behind()` (на `pending_plan`), той самий, що й у знімку §9.7; для `public` — `migrator.public_plan()`. Оренди читаються одним запитом (`migrator.lease_holders()`).
  - `--check` перевіряє ACTIVE на **усіх** шардах, включно з `is_active=False`, хоча `--tenant` такі шарди пропускає (§9.3). Відсталий тенант на неактивному шарді валить гейт з підказкою `migrate_schemas -s X`: код, що викочується, не повинен працювати зі старою схемою.
  - Недоступний шард перевіряється один раз, решта його тенантів пропускається (без N таймаутів підключення).
  - Колонки `--report`: SCHEMA, SHARD, STATUS, BEHIND (n / `no schema` / `?`), LEASE (owner, since, EXPIRED), NEXT; під рядком — примітки (PENDING у роботі/застряг, INVALID-індекси, останній рядок `last_error` для FAILED) і з `--plan` імена міграцій.
  - Вартість: ~6 мс на тенант локально (Docker); через RDS Proxy додається перепідключення до шарду на кожного тенанта (`_close_shard` у `pending_plan`).

---

## 12. Заборона `migrate` у MT і test runner

Код виходу 1 (стандартний для `CommandError`):
```python
# tenants/test_runner.py
_creating_test_dbs = False
class TenantsTestRunner(DiscoverRunner):
    def setup_databases(self, **kw):
        global _creating_test_dbs
        _creating_test_dbs = True
        try: return super().setup_databases(**kw)
        finally: _creating_test_dbs = False

# tenants/management/commands/migrate.py
from django_tenants.management.commands.migrate_schemas import MigrateSchemasCommand
class Command(TenantCommand, MigrateSchemasCommand):
    def handle(self, *a, **o):
        from tenants import test_runner
        if not test_runner._creating_test_dbs:
            raise CommandError("manage.py migrate is disabled in multi-tenant mode; use migrate_schemas "
                               "(see deploy/migrations_zero_downtime.md).")
        return super().handle(*a, **o)
```
- `settings_multitenant.py`: `TEST_RUNNER = "tenants.test_runner.TenantsTestRunner"`.
- `create_test_db` викликає `call_command("migrate", run_syncdb=True, …)` (Django `db/backends/base/creation.py`, ~`:78`) і проходить, бо прапорець виставлено. Парсер аргументів — від upstream `MigrateSchemasCommand`. `TenantCommand` імпортується з `commons.platform.commands` і стоїть першим у MRO, як у наявному `tenants/management/commands/migrate_schemas.py:59,77` (його `execute()` виконує guard `USE_MULTITENANT`). Upstream `django_tenants/management/commands/migrate.py` — це просто `Command = MigrateSchemasCommand`, тому наш `migrate` у `tenants` його перекриває.
- `'tenants'` стоїть перед `'django_tenants'` у `SHARED_APPS`, тому команда `migrate` береться з `tenants`.
- `'migrate'` іде в `TENANT_COMMANDS`; `tenant_command migrate` → помилка з підказкою.
- `testserver` у MT не підтримується.
- **Реалізовано (крок 6, 2026-10-12)** як написано вище. Виклики `migrate` у коді проєкту не знайдено (лише `create_parser("manage.py", "migrate")` для дефолтів парсера). `run_migrations` бере базову команду через `get_tenant_base_migrate_command_class()` (Django `migrate`), тож override його не зачіпає.

---

## 13. System checks і CI-лінт

`tenants/checks/migrate.py` (лише MT):

| ID | Умова | Рівень |
|---|---|---|
| E020 | задано будь-який `TENANT_MULTIPROCESSING_*` | Error |
| W021 | задано env `EXECUTOR` | Warning |
| E022 | `TENANT_MIGRATE_JOBS*` не int, bool або < 1; `PER_SHARD > JOBS` | Error |
| E024 | `TENANT_MIGRATE_DDL_LOCK_TIMEOUT` не відповідає `^(0\|\d+(ms\|s\|min))$` | Error |
| E029 | `TENANT_MIGRATE_LOG_DIR` не задано або не шлях | Error |

**CI-тест `test_migration_lint.py`** (замість W025/W026, щоб не сканувати файли на кожному `manage.py`):
- міграції застосунків проєкту з `atomic = False` мають першою операцією `LockTimeoutOff`, а останньою — `LockTimeoutRestore`;
- `CONCURRENTLY IF NOT EXISTS` і `AddIndexConcurrently` заборонено.
- **Додано після ревю (2026-10-13):** лінт бачить і `atomic: bool = False` (анотоване присвоєння раніше пропускалось); перед кожним `CREATE [UNIQUE] INDEX CONCURRENTLY "<ім'я>"` у тій самій міграції має стояти `DROP INDEX CONCURRENTLY IF EXISTS "<ім'я>"` (інакше INVALID-залишок невдалого CIC валить кожен повтор з «already exists»). Docstring `migration_ops` тепер явно розділяє, що перевіряє лінт, а що — ні (унікальність імені між міграціями, SQL із f-рядків).

**Реалізовано (крок 6, 2026-10-12):** `tenants/checks/migrate.py` (одна функція, усі п'ять ID); лінт — `commons/tests/test_migration_lint.py`, статичний (`ast`), сканує `*/migrations*/[0-9]*.py` проєкту, включно з канаркою, і перевіряє сам себе на поганих зразках. `commons/tests/` додано до міток standalone у `scripts/ci_mode.sh`.

---

## 14. Стопер реактивації, адмінка, консоль, middleware

### 14.1 Хто що називає
- **Консоль** — DRF `TenantViewSet` (`tenants/console/views.py`, публічний хост, `urls_public.py:22`). Нею користується фронтенд.
- **Адмінка** — Django admin `public_admin_site` (`tenants/console/admin.py`).

Обидві живуть у пакеті `tenants/console/`.

### 14.2 Адмінка (`TenantAdminForm` / `TenantAdmin`)
Матриця переходів (`console/admin.py:150-160`):

| Поточний | Дозволені |
|---|---|
| NEW | NEW (поле заблоковане) |
| PENDING | PENDING (поле заблоковане) |
| ACTIVE | ACTIVE, DEACTIVATED, **MAINTENANCE** |
| DEACTIVATED | DEACTIVATED, ACTIVE, **MAINTENANCE** |
| FAILED | FAILED, NEW, **MAINTENANCE** |
| **MAINTENANCE** | MAINTENANCE, ACTIVE, DEACTIVATED, FAILED |

**`clean()`** при зміні статусу:
- → ACTIVE (з DEACTIVATED або MAINTENANCE): `check_reactivation()`. При `SchemaBehind`, `SchemaMissing`, `DeployInProgress` або `ShardUnavailable` → `ValidationError` з підказкою `migrate_schemas -s X` (для MAINTENANCE: `-s X --maintenance`).
- → MAINTENANCE: якщо оренду тримає живий мігратор → `ValidationError("зараз мігрує <owner> з <since>; зачекайте")`.
- FAILED → NEW: обов'язкова галочка `confirm_schema` з підписом «Я перевірив схему: дані відновлено, або я приймаю порожню схему, або наявна схема належить цьому тенанту». Поруч показується поточний стан схеми: немає, порожня, непорожня (n таблиць).

**`save_model()`:**
- статус змінюється CAS-ом `filter(pk, status=<старий>).update(status, previous_status=<старий>, status_changed_at=now, last_error='' для NEW)`;
- 0 рядків → `transaction.set_rollback(True)` + повідомлення «статус змінився паралельно, оновіть сторінку»;
- 1 рядок → `transaction.on_commit(forget_tenant)`;
- інші поля зберігаються через `obj.save(update_fields=<змінені, крім status і confirm_schema>)`.

**Реалізовано (крок 7, 2026-10-12):**
- Матриця — `STATUS_TRANSITIONS` у `tenants/console/admin.py`; тексти помилок англійською, як решта адмінки.
- → MAINTENANCE відмовляє, якщо оренда жива **або** протухла, але на шарді ще працює процес `tm:<pk>` (той самий критерій «мігрує зараз», що й у `migration_lease`).
- **Додано після ревю (2026-10-13):** перевірка у формі й запис статусу — окремі кроки, і мігратор міг узяти оренду між ними (перечитавши ще не закомічений ACTIVE). Тепер `save_model` перед CAS → MAINTENANCE викликає `migrator.lease_lock_for_transition(pk)`: `SELECT … FOR UPDATE` рядка оренди в транзакції адмінки (рядок створюється вільним, якщо його немає). `lease_acquire` мігратора чекає на цей рядок до коміту, потім бачить MAINTENANCE і пропускає тенанта (`status_maintenance`); якщо мігратор устиг першим — адмінка відмовляє (`StatusConflict`, повідомлення + редирект). Порядок блокувань (рядок оренди → рядок тенанта) той самий, що в мігратора. Тести: `MaintenanceEntryRaceE2E` (два справжні з'єднання; без блокування мігратор запускає міграції — перевірено ін'єкцією), `AdminTests.test_maintenance_refused_when_a_migrator_got_the_lease_after_clean`. Перевірку `migration_lease --list` у runbook лишаємо як додаткову.
- `confirm_schema` показується лише для FAILED (`get_fields`); стан схеми рахує `migrator.schema_table_count()`.
- **Виправлено після ревю (2026-10-12).** Перша версія мала два дефекти, не видимі в тестах, що викликали `save_model` напряму:
  (а) CAS 0 рядків → `set_rollback(True)`, але Django далі в тій самій транзакції викликав `save_related` і `log_change` → `TransactionManagementError` (500);
  (б) адмінка перечитує рядок на POST, тож «старий статус» був свіжим, і сторінка, відкрита до чужої зміни статусу, мовчки її перезаписувала.
  Тепер: приховане поле `expected_status` (статус, який бачив оператор, з GET) — база переходу і CAS. Статус змінився до POST → помилка форми, нічого не пишеться. Змінився між `clean()` і CAS → `save_model` кидає `StatusConflict`, виняток виходить з `atomic()` адмінки (відкат, `log_change` не виконується), `changeform_view` перетворює його на повідомлення + редирект. Якщо оператор статус не змінював, статус не пишеться взагалі (чужа зміна зберігається). Тести — `AdminChangeViewTests` через повний `changeform_view`; тест гонки перевірено ін'єкцією старої поведінки.
- Зміна інших полів **ніколи** не пише `status`: раніше `obj.save()` повністю перезаписував рядок і міг повернути застарілий статус поверх запису мігратора.

### 14.3 Консоль (`TenantViewSet`)
| Дія | Поведінка |
|---|---|
| `activate` (DEACTIVATED → ACTIVE, `views.py:398`) | спершу `check_reactivation`: 409 `schema_behind` (з `pending`), 409 `schema_missing`, 503 `deploy_in_progress`, 503 `shard_unreachable` |
| `activate` / `deactivate` для MAINTENANCE | CAS у `_transition` і так оновить 0 рядків; додаємо зрозумілу відповідь 409 `tenant_maintenance`: «tenant is in maintenance; managed by platform admin» |
| `provision` | лише NEW, без змін |
| `DELETE /tenants/{id}/` для MAINTENANCE | **відмова** 409: «finish maintenance first». Інакше `drop_tenant_schema_task` міг би видалити схему посеред `pg_restore` |
| входу в MAINTENANCE і виходу з нього | немає |
| Перевірка фізичної схеми при створенні (`console/serializers.py:215-243`) | **лишається**: відмовляє ще до створення тенанта і дає користувачеві зрозуміле повідомлення |

Текст підказки `views.py:291-299` змінюється: «reset it via reconcile_tenants» → «admin FAILED → NEW».

**Виправлено після ревю (2026-10-13):** `PATCH` (`TenantSerializer.update`) раніше робив повний `save()` рядка, прочитаного на початку запиту, і повертав застарілі `status`/`previous_status`/`last_error` поверх паралельної зміни (захоплення мігратором, MAINTENANCE з адмінки). Тепер — `save(update_fields=<лише змінені поля>)` (`ConsolePatchTests`).

**Реалізовано (крок 7, 2026-10-12):** 409 для MAINTENANCE — виняток `TenantMaintenance` (DRF `APIException`, тіло `{"detail", "code": "tenant_maintenance"}`), спільний для `activate`, `deactivate` і `DELETE`. Стопер працює лише в `activate`; тіло відповіді — `{"detail", "code"}` (+ `pending` для `schema_behind`). Підказку `views.py` змінено ще в кроці 5.

### 14.4 Middleware (`tenants/middleware.py:100-120`)
MAINTENANCE → 503 `tenant_maintenance` з `Retry-After`. Власна `/admin/` тенанта лишається доступною, як для FAILED.

**Реалізовано (крок 7, 2026-10-12):** `Retry-After: 600` (у плані значення не було; обслуговування довше за провізіонування з його 300), HTML-шаблон `tenants/errors/maintenance.html`.

---

## 15. Інші точки входу

### 15.1 Celery `provision_tenant` (`tenants/tasks.py`)
- Фільтр: `status in ("new", "pending")`, інакше skip.
- Викликає `migrator.migrate_tenant(pk, RunOptions())` **напряму**, без `call_command`, і отримує `Result`. Лог-файлів немає.
- skipped `locked` або `code_older_than_db` → `self.retry(countdown=300, max_retries=15)`. Так закривається питання «старий воркер під час викатки»: задачу підхоплює новий воркер. Це ж відновлює застряглий PENDING поза деплоєм.
- failed → ERROR у лог, без retry: з тенантом розбираються окремо.
- Повторна доставка (`acks_late`) механізмом відновлення не є.
- З'єднання — за §7.
- **Реалізовано (крок 7, 2026-10-12):** `PROVISION_RETRY_COUNTDOWN = 300`, `PROVISION_MAX_RETRIES = 15` у `tenants/tasks.py`. Задача повертає `{"tenant_id", "schema", "kind", "reason", "msg"}` (раніше `{"schema", "status", "skipped"}`; споживачів результату в коді немає). Тенант, якого немає або статус не NEW/PENDING, → `kind: skipped, reason: not_provisionable` без виклику ядра. `CLAIMABLE_STATUSES` лишився тільки в `migrate_schemas_legacy.py`; автовиправлення `reconcile_tenants` видалено в кроці 5.

### 15.2 `bootstrap_tenant`
Без змін: `call_command("migrate_schemas", schema_name=…)` у послідовному режимі. Захист від сирітської схеми забезпечує M4 у мігратора.

### 15.3 `create_tenant_schema` / `drop_tenant_schema`
Без змін і без мютексу (Q7):
- гонка в `create` закінчується видимою помилкою (`CREATE SCHEMA` без `IF NOT EXISTS`, `:59`) або повідомленням «already exists»;
- `drop` працює лише з тенантами, яких уже немає в реєстрі (`:62-67`), тож з мігратором не перетинається;
- рідкісне вікно «тенанта видаляють посеред міграції» серіалізує PostgreSQL: `DROP SCHEMA` чекає на блокування мігратора, а CAS мігратора повертає `tenant_deleted`.

---

### 15.4 `migrate_schemas_legacy` (стара команда)
Поточний `tenants/management/commands/migrate_schemas.py` (320 рядків, перевірено 2026-10-12) **копіюється** у `migrate_schemas_legacy.py` **до** переписування `migrate_schemas` (крок 3 §21). Це запасний інструмент на випадок, якщо новий мігратор поводиться неочікувано.

**Що зберігається без змін:** уся логіка старої команди:
- `CLAIMABLE_STATUSES = (NEW, ACTIVE, DEACTIVATED)` (константа лишається **всередині** цього файлу; з решти коду її прибираємо);
- захоплення CAS-ом `→ PENDING` з `previous_status=F("status")`, створення схеми для NEW, `schema missing` → FAILED;
- запуск через upstream-виконавця (`GET_EXECUTOR_FUNCTION`, `--executor`), фіналізація `PENDING → previous_status` (NEW → ACTIVE);
- помилка → FAILED + `forget_tenant` + raise;
- прапорці upstream (`--plan`, `--list`, `--check` тощо) і вибір шарду за `Tenant.shard.alias`.

**Єдина зміна (рішення 2026-10-12: тимчасовий запасний варіант, «нехай лишається як є»):** модульний docstring і `help` починаються з `LEGACY:`, посилаються на цей розділ і перелічують відмінності від нової команди. Попередження в stderr і запобіжника за орендою **немає**; ні `lock_timeout`, ні лог-файлів, ні `--summary-json`, ні MAINTENANCE-логіки. MAINTENANCE і FAILED не потрапляють у `CLAIMABLE_STATUSES`, тож стара команда такі тенанти пропускає (для `-s X` з повідомленням «already claimed by another process», яке тут вводить в оману).

Наслідок: стара команда не бере оренду і може мігрувати той самий тенант паралельно з новим мігратором або Celery. Від цього захищає лише ручна перевірка `migration_lease --list` перед запуском (§19).

**Чим стара команда відрізняється від нової (оператор має це знати):**
| | `migrate_schemas` (новий) | `migrate_schemas_legacy` |
|---|---|---|
| Оновлення ACTIVE/DEACTIVATED | статус не змінюється, тенант працює | тенант у PENDING → **веб віддає 503** до кінця його міграції |
| Помилка оновлення | статус не змінюється | FAILED (потрібне ручне FAILED → NEW в адмінці) |
| Мютекс | оренда `MigrationLease` + огороджений CAS | лише CAS статусу; оренду не бере і чужу не перевіряє |
| `lock_timeout` | 5 с на кожен тенант | немає (DDL може чекати блокування необмежено) |
| Перша помилка | `--keep-going` / `--max-failures` | `raise`, прогін зупиняється |
| Паралельність | власний fork-пул | upstream `--executor=multiprocessing` (див. недоліки upstream) |

**Хто викликає:** тільки людина вручну. Celery, `bootstrap_tenant`, консоль, CI-пайплайн і документація деплою стару команду **не** використовують. Тест перевіряє, що в коді немає викликів `call_command("migrate_schemas_legacy", …)`.

**System checks.** E020/W021 (§13) лишаються: вони стосуються налаштувань, а не команди. Тому `--executor=multiprocessing` у legacy працює лише з дефолтами upstream (2 процеси).

**Видалення:** окремим рішенням користувача, коли новий мігратор відпрацює кілька релізів. До того часу файл не змінюємо, крім docstring і `help`.

## 16. Інвалідація кешу

| Подія | Інвалідація |
|---|---|
| NEW → PENDING, PENDING → PENDING | ні (503 в обох випадках) |
| PENDING → ACTIVE / FAILED, NEW → FAILED (`schema_exists`), UPGRADABLE → FAILED (`schema_missing`); CAS змінив 1 рядок | так |
| CAS змінив 0 рядків, `BaseException`, skipped без CAS | ні |
| Оновлення ok/noop/failed | ні |
| Консоль `activate`/`deactivate` | так (уже є) |
| Адмінка CAS статусу (включно з MAINTENANCE) | так, через `on_commit` |
| Адмінка, інші поля | так, через `post_save` |

---

## 17. Інфраструктурні вимоги та топологія

- **Потрібна сесійна семантика з'єднань.** django-tenants перемикає схему сесійним `SET search_path`, мігратор ставить `set_config(…, false)`. Будь-який пулер має працювати в session-режимі. RDS Proxy (зараз обов'язковий) на таких `SET` пінить сесію, тому підходить. PgBouncer — лише session mode.
- **Від специфіки проксі мігратор не залежить:** ні від деталей пінінгу, ні від поведінки бекенда після SIGKILL клієнта, ні від передачі cancel. Оренда і огородження — звичайні `INSERT`/`UPDATE`. `application_name` ставиться SQL-запитом, тож його видно через будь-який сесійний пулер.
- **Обмеження пулера:**
  - з'єднання мігратора не повинно простоювати довше за idle-timeout пулера. Міграція, яка довго рахує в Python без запитів, — анти-патерн: бекфіли робимо окремо, батчами;
  - міграція одного тенанта, довша за максимальний час життя клієнтського з'єднання (RDS Proxy: 24 год), не підтримується.
  - **(ревю 2026-10-12)** міграція одного тенанта, довша за `LEASE_TTL` (1 год), теж не підтримується: перевірка M1 (`state <> 'idle'`) не бачить живого власника, коли той між двома запитами поза транзакцією (між міграціями, між кроками `atomic=False`), тож протухлу оренду може перехопити інший прогін. Повне рішення — сесійний advisory lock на з'єднанні шарду — свідомо не беремо (ред. 7 пішла від залежності від поведінки проксі); повертатися, якщо такі міграції з'являться.
  - **(ревю 2026-10-13)** TCP keepalive (`_TCP_KEEPALIVES` у `_proxy_db_options()`/`_aurora_db_options()`): «тихо мертве» з'єднання виявляється за ~60 с і дає помилку з'єднання, а не вічне очікування. Звернення мігратора до `default` (оренда, CAS, читання) свідомо **без** `lock_timeout`/`statement_timeout`: сесію `default` далі використовує решта коду процесу, а транзакційно-локальний таймаут ускладнив би ядро заради ручного випадку. Натомість правило в runbook: не тримати відкритих транзакцій на `tenants_tenant`/`tenants_migrationlease`.
  - **(ревю 2026-10-12)** сесія `tm:<key>` іншої ролі (без `pg_read_all_stats`) має `state = NULL`: запит M1 рахує її зайнятою (`coalesce(state, 'unknown') <> 'idle'`), тест `HiddenSessionStateTests`.
- **Бюджет з'єднань:**
  - на шард — `≤ jobs_per_shard` міграційних з'єднань плюс короткі перевірки (`pending_plan`, `shard_session_active`);
  - до `default` — `≤ jobs` коротких з'єднань (закриваються після кожного звернення).
- **`deploy/DATABASE_SETUP.md:362-367`** («we deliberately skipped RDS Proxy») застарів: переписати (§18). **Зроблено в кроці 8** (`DATABASE_SETUP.md` §9, підрозділ «Pooler»).

---

## 18. Документація

**Реалізовано (крок 8, 2026-10-12):**
- `deploy/migrations_zero_downtime.md` (новий, українською, як інші дизайн-документи `deploy/`): пункти 1–12 нижче + runbook §19 як його §13 (з двома додатковими рядками: FAILED після виправлення причини; Celery retry з лімітом).
- `deploy/UPSTREAM_FORK.md`: рядок 5 мапи (handle — власний мігратор, виконавці upstream не використовуються), новий рядок 9 (`migrate` + test runner), номери рядків у §4, новий §4a з таблицею залежностей мігратора, §7 (дифати `migration_executors/base.py`), §8 (нові канарки).
- Канарки §18.2 — `MigratorUpstreamContractTests` у `tenants/tests/test_upstream_contract.py` (6 тестів; префікс і закриття з'єднання — поведінкові, на справжньому `run_migrations`; канарку префікса перевірено ін'єкцією поломки).
- `deploy/DATABASE_SETUP.md` (англійською, як і раніше): §7b, §9 (пулер), §10, §11.
- `README.md` (англійською): статусна машина, команди, Step 7 — **міграції перед кодом** (раніше було навпаки: спершу рестарт gunicorn), Step 9–10. `deploy/README.md` §3.4: `migrate_schemas` + `reconcile_tenants --check` перед рестартом.
- **Не змінено:** `tenants_back/settings_local_multitenant.py.example` досі за замовчуванням використовує `_aurora_db_options()` і лише в коментарі згадує `_proxy_db_options()` — це налаштування, а не документація; рішення за користувачем.

### 18.1 `deploy/migrations_zero_downtime.md`
1. Порядок деплою; що робити при коді 0 і коді 1; рядок `Stop:`.
2. **Вимога до деплою: concurrency-група CI** для пайплайну міграцій. Два одночасні прогони не допускаються, і `--shared` при зайнятій оренді одразу падає.
3. **Вимога до деплою: тека логів.** Якщо `migrate_schemas` виконується в одноразовому контейнері, `TENANT_MIGRATE_LOG_DIR` має бути змонтованим томом (або її збирає лог-колектор), інакше логи зникнуть разом з контейнером. Також: структура тек, скільки зберігається (дві попередні плюс поточна), де шукати файл тенанта.
4. Правила expand-only:
   - `null=True` або `db_default`;
   - без перейменувань;
   - видалення поля — у два релізи (`SeparateDatabaseAndState`);
   - FK без індексу, індекс — окремою CIC-міграцією;
   - constraint через `NOT VALID` + `VALIDATE`;
   - без `AlterField` зі зміною типу;
   - бекфіли — окремо, батчами;
   - застосовані міграції не редагуються;
   - фіча-флаг не захищає від ORM.
5. **Правило:** міграцій, які одночасно чіпають схему тенанта і `public`, немає (Q3).
6. **Правило:** міграції та `post_migrate`-обробники не закривають і не перевідкривають з'єднання (§7).
7. `lock_timeout`: що це, де діє, 55P03 при оновленні і при провізіонуванні.
8. Рецепт `CONCURRENTLY` (§8.2), заборона `IF NOT EXISTS`, INVALID-індекси.
9. Переривання: перший сигнал — дренаж, другий — жорстка зупинка; оренда після SIGKILL; `migration_lease`.
10. Паралельність і бюджет з'єднань. `--summary-json`: формат (§9.10), як CI читає `exit_code`, `stop` і `results[].reason`; файл варто зберігати як артефакт CI-джоби.
11. Заборони: `manage.py migrate`, `testserver`, `--executor`, `TENANT_MULTIPROCESSING_*`, `migrate_schemas --check/--list/--plan`. Окремий абзац про `migrate_schemas_legacy`: що це, коли можна (лише вручну, коли новий мігратор поводиться неочікувано), таблиця відмінностей з §15.4, особливо 503 на час оновлення тенанта.
12. Статус MAINTENANCE і сценарій відновлення; карантин через DEACTIVATED.

### 18.2 `deploy/UPSTREAM_FORK.md` (оновити `:37`, `:101`)
Від чого залежимо в upstream і стандартній бібліотеці:
- `run_migrations` закриває з'єднання лише після успіху (`base.py:63-72`);
- пастка `include_public` (`base.py:47-49`);
- префікс рядків `[executor:schema]` (`base.py:22-38`);
- поведінка psycopg на `KeyboardInterrupt`: чекає кінця запиту, `transaction_status` (`psycopg/connection.py:451-463`);
- `multiprocessing.active_children()`;
- `prctl` (лише Linux);
- `changeform_view` під `atomic`.

Тести контракту — у `test_upstream_contract.py`.

### 18.3 `deploy/DATABASE_SETUP.md`
Розділ про пулер переписати:
- потрібна сесійна семантика;
- RDS Proxy обов'язковий;
- `_proxy_db_options()`;
- PgBouncer допустимий лише в session mode;
- посилання на §17.

---

## 19. Runbook

Робоча копія для операторів — `deploy/migrations_zero_downtime.md` §13 (крок 8). Зміни runbook вносимо туди.

| Ситуація | Дія |
|---|---|
| Код 0 після `--tenant` | `reconcile_tenants --check`; код 0 → викатка |
| Код 1, failed `migration_error` | лог тенанта в теці прогону → виправити й накотити вперед (fix-forward) → `migrate_schemas -s X` або `--tenant --keep-going` |
| Код 1, failed `lock_timeout` | довгі транзакції в `pg_stat_activity` на шарді → повтор |
| Код 1, skipped `locked` | `migration_lease --list`; власник живий → дочекатися; мертвий → `--release` → повтор |
| `shared stage busy` | паралельний деплой або ручний запуск → дочекатися; мертвий прогін → `migration_lease --release shared` |
| Тенант не мігрується, а реліз потрібен | deactivate (карантин) → `--check` → викатка → виправлення → `migrate_schemas -s X` → activate |
| Застряглий PENDING | `migrate_schemas -s X` (або Celery retry). Якщо оренда висить → `migration_lease --release X` |
| Оренда після SIGKILL | `migration_lease --release X` (перевіряє активну сесію). `--force` — лише якщо бекенда гарантовано немає |
| INVALID-індекс | `DROP INDEX CONCURRENTLY "<schema>"."<idx>"` → повтор |
| FAILED `schema_missing` | відновити зі snapshot/PITR (через MAINTENANCE) або адмінка FAILED → NEW з галочкою |
| FAILED `schema_exists` | перевірити, чия це схема. Сирітська → `drop_tenant_schema`, потім адмінка FAILED → NEW. Належить тенанту → адмінка FAILED → NEW з галочкою |
| **Відновлення тенанта з бекапу** | адмінка → MAINTENANCE → `migration_lease --list`: немає оренди й активної сесії для тенанта → DBA `drop`/`create` схеми + `pg_restore` → `migrate_schemas -s X --maintenance` → `reconcile_tenants --report -s X` → адмінка → ACTIVE |
| Жорстка зупинка | знімок у підсумку → `reconcile_tenants --report` → `migration_lease --list` → повтор |
| Веб ловить нестачу з'єднань під час прогону | дренаж (Ctrl-C), зменшити `--jobs`/`--jobs-per-shard` |
| Celery `provision_tenant` skipped `locked` | нічого: retry |
| Новий мігратор поводиться неочікувано, реліз потрібен | **обов'язково** `migration_lease --list` (має бути порожньо; інших захистів у legacy немає) → `migrate_schemas_legacy --tenant` (або `-s X`). Пам'ятати: кожен тенант на час своєї міграції віддає 503, помилка → FAILED |

---

## 20. Тести

### 20.1 Розміщення
- Тести без БД: `tenants/tests/test_migrator_*.py` (SimpleTestCase).
- З БД: `tenants/tests/db_migrator_core.py` (TestCase), `db_migrator_e2e.py` (TransactionTestCase).
- `tenants/tests/settings_db.py`: `default`, `tenant_1`, `tenant_2` на одному postgis. **Відхилення (крок 6, 2026-10-12):** канарку додає не він, а окремий `tenants/tests/settings_db_canary.py` (`TENANT_APPS + ["tenants.tests.canary"]`). Причина: 0002 виконує `CREATE INDEX CONCURRENTLY`, а TestCase загортає все в транзакцію, тож кожен TestCase, що провізіонує справжнього тенанта, падав би. Під `settings_db_canary` запускаються лише TransactionTestCase (`db_migrator_e2e.py`); канаркові класи без неї пропускаються. Для `StandaloneCanaryE2E` — `settings_db_standalone.py` (`USE_MULTITENANT=0`, `tenants/tests/db_standalone_canary.py`). Тестовий застосунок-канарка `tenants.tests.canary` з міграціями:
  - 0001 атомарна: `CreateModel canary_item` (`id`, `name`);
  - 0002 `atomic=False` за рецептом §8.2: CIC-індекс на `canary_item.name`;
  - 0003 `RunPython`: записує `current_setting('lock_timeout')` у таблицю `canary_probe` (тест перевіряє, що там `'5s'`);
  - 0004 `AddField(canary_item.extra, null=True)`: справжній `ADD COLUMN`, потрібен `LockTimeoutE2E`.
- Окремий `MIGRATION_MODULES`-варіант канарки лише з 0001–0003 (для `LockTimeoutE2E`): `tenants/tests/canary/migrations_upto3/` реекспортує ті самі класи `Migration`, без копій коду.
- `TENANT_MIGRATE_LOG_DIR` — тимчасова тека.

### 20.2 SimpleTestCase
| Клас | Що перевіряє |
|---|---|
| `PickNextTests` | ліміти jobs/per_shard; коло; лічильник у порядку завершення; без `--keep-going` → fail-fast; `conn_error=shard` → решта шарду `not run`, лічильник не росте; `conn_error=default` → глобальна зупинка |
| `ClassifyTests` | `08*`, `57P01-3`, sqlstate `None`, `InterfaceError`, ланцюжки; `55P03` → `is_lock_timeout` |
| `FlagTests` | `--check`/`--list`/`--plan` → підказка, код 1; targeted без цілі → помилка; `--maintenance` без `-s` → помилка; `--executor=multiprocessing` → помилка; невалідні jobs/max-failures |
| `OwnerTests` | `owner()` у fork-нащадку ≠ батьківського; повторний виклик у тому самому процесі стабільний; формат `host:pid:start` |
| `ExitCodeTests` | 0 лише при всіх ok/noop без зупинки; `locked` → ok після повторної перевірки → 0 |
| `SummaryJsonTests` | структура `version: 1`; `exit_code` = код процесу; `counts` після §9.5 і `rechecked`; `stop` для кожної причини; `not_run`/`interrupted` зі `snapshot`; атомарний запис (`.tmp` + `os.replace`); недоступна тека → `CommandError` до прогону; `-` відхиляється; список кодів `reason` збігається з константами |
| `ChecksTests` | E020, W021, E022, E024, E029 |
| `MigrateOverrideTests` (крок 6: `tenants/tests/test_migrate_override.py`, разом з `ChecksTests`) | команда `migrate` береться з `tenants`; без прапорця test runner → помилка; з прапорцем делегує |
| `ParallelTests` (крок 4) | справжній fork з замоканим `migrate_tenant`: воркери мають SIG_IGN, PDEATHSIG=SIGKILL, line buffering; ліміт `--jobs-per-shard`; виняток у воркері → failed; 1-й сигнал → дренаж; 2-й → SIGKILL воркерів, `interrupted` + `snapshot`, живих дітей не лишилось; убитий воркер → `broken_pool`; одна ціль → без fork |
| `SignalTests` (крок 4) | послідовний режим: 1-й сигнал → дренаж; 2-й → `hard_stop`, код 1, знімок; сигнал під час `--shared` → етап тенантів не починається; `call_command` не чіпає SIGINT; обробники відновлюються |
| `LegacyCommandTests` | `migrate_schemas_legacy` знаходиться, є `TenantCommand`, docstring і `help` починаються з `LEGACY:`; у коді немає викликів legacy |
| `StandaloneTests` (крок 6: `commons/tests/test_migration_ops.py`, разом з `MigrationOpsTests`; у MT пропускається) | `USE_MULTITENANT=0`: `migrate` = `django.core`; імпорт `migration_ops`; `TENANT_MIGRATE_DDL_LOCK_TIMEOUT is None`; `tenants` не в `INSTALLED_APPS` |
| `MigrationOpsTests` | `deconstruct()`; `_restore(None)` → `SET lock_timeout TO DEFAULT` |
| `MigrationLint` | CI-лінт (§13) |

### 20.3 TestCase
| Клас | Що перевіряє |
|---|---|
| `LeaseTests` | захоплення; зайнята; протухла без сесії → перехоплення; протухла з активною сесією `tm:<pk>` → skipped (M1); CAS за старим власником (двоє претендентів → один); огородження з чужим owner → 0 рядків; звільнення лише свого; `to_regclass` → `--shared` без оренди |
| `PendingPlanTests` (B1) | схеми немає → `MISSING` при наявній `public.django_migrations`; порожня схема → повний план; змігрована → `[]` |
| `ProvisionTests` | NEW → ACTIVE + forget 1 раз; `previous_status`; PENDING без власника → RESUME; обрив з'єднання → PENDING; інша помилка, включно з 55P03 → FAILED + forget; KeyboardInterrupt при idle-бекенді звільняє оренду, при ACTIVE — ні; `code_older_than_db`; `Incomplete`; **M4**: непорожня схема → FAILED `schema_exists`; з `previous_status=failed` → провізіонування приймає схему; порожня схема → звичайний шлях |
| `UpgradeTests` | NOOP без оренди і без записів у `default`; failed без запису статусу; `schema_missing` → FAILED (ACTIVE/DEACTIVATED); targeted |
| `MaintenanceTests` | MAINTENANCE не береться `--tenant`; `-s X` без прапорця → skipped; з `--maintenance` → мігрує без запису статусу; схеми немає → failed без запису; адмінка відмовляє входу при живій оренді; консоль `DELETE` → 409; middleware → 503, `/admin/` доступна |
| `CeleryConnectionHygieneTests` (§7; крок 7: TransactionTestCase у `db_migrator_e2e.py` — у TestCase `_close_shard` лише скидає, а не закриває) | після `provision_tenant` (успіх і помилка) на тому самому alias `SHOW lock_timeout` = дефолт, `application_name` не `tm:*`; те саме для `default` і `--shared` |
| `FreshDefaultInAtomic` | `_fresh()` в `atomic` нічого не закриває |
| `ReconcileTests` (крок 5: `tenants/tests/db_lease_reconcile.py`, разом з `MigrationLeaseCmdTests`) | `--check` нічого не пише; `MISSING` → 1; MAINTENANCE/DEACTIVATED не перевіряються; WARN stuck; `--report --plan` показує імена |
| `MigrationLeaseCmdTests` | `--list` з «tenant deleted»; `--release` відмовляє при активній сесії; `--force` |
| `AdminTests` (крок 7: `tenants/tests/db_admin_console.py`, разом з `ConsoleStopperTests` і `CheckReactivationTests`) | матриця з MAINTENANCE; → ACTIVE зі стопером; FAILED → NEW без галочки → невалідна; CAS 0 → відкат + повідомлення |
| `ConsoleStopperTests` | 409 / 409 / 503 / 503 / 200; MAINTENANCE → 409 `tenant_maintenance` |
| `TasksTests` (крок 7: `tenants/tests/test_tasks_provision.py`, без БД) | прямий виклик ядра; `locked`/`code_older` → retry; failed → без retry |
| `RunLogTests` | файли `_shared.log`, `<schema>.log`, `_summary.log` (схема `summary` має власний файл; небезпечне legacy-ім'я → `_tenant_<pk>.log`); повний traceback; зберігання двох попередніх тек; Celery файлів не пише |

### 20.4 TransactionTestCase
| Клас | Що перевіряє |
|---|---|
| `ProvisionE2E` (крок 6, разом з `ReentryE2E` в одному тесті) | NEW → ACTIVE з 0001–0004; 0003 записала `'5s'`; INVALID-індексів немає |
| `ParallelE2E` | `--jobs=2`, 4 тенанти на 2 шардах → ok, код 0; рядки воркерів цілі (line buffering). Реалізовано (крок 4) у `tenants/tests/db_migrator_e2e.py`: fd 1 перехоплюється, кожен надрукований рядок має бути цілим рядком файлу якогось тенанта; тенантів провізіонували воркери (owner у шапках ≠ owner батька). Поруч у `db_migrate_schemas.py`: `SnapshotTests` для `migrator.snapshot()` |
| `LockTimeoutE2E` | **Підготовка:** NEW-тенант `x` провізіонується з варіантом канарки 0001–0003 (`override_settings(MIGRATION_MODULES=…)`), потім повний набір повертається, і 0004 стає справжнім незастосованим `ADD COLUMN`. **Дія:** друге з'єднання `BEGIN; SELECT * FROM x.canary_item;` (тримає `AccessShareLock`); `call_command("migrate_schemas", schema_name="x")`. **Очікування:** failed `lock_timeout` за 5 ± 1 с, статус ACTIVE, `CommandError` (код 1); третє з'єднання під час очікування робить `SELECT` з `canary_item` і не блокується довше за таймаут. Після `ROLLBACK` другого з'єднання повтор → ok |
| `ReentryE2E` | повтор → noop |
| `StandaloneCanaryE2E` | `USE_MULTITENANT=0`: міграція з `LockTimeoutOff/Restore` застосовується Django `migrate` |

### 20.5 Docker smoke (без проксі)
1. Два одночасні прогони → `locked` + повторна перевірка → обидва з кодом 0; другий `--shared` одразу «busy».
2. `kill -9` воркера або батька (PDEATHSIG) → оренда висить → `migration_lease --release` відмовляє, поки бекенд активний → після завершення звільняє → повтор ok.
3. Ctrl-C ×1 (дренаж) і ×2 (жорстка зупинка) з `--jobs=2` і `--jobs=1`.
4. `docker stop` шарду 2 → решта шарду `not run`, шард 1 ok, код 1.
5. Сценарій відновлення: MAINTENANCE → `pg_restore` → `-s X --maintenance` → `--report` → ACTIVE.

**Результат (крок 9, 2026-10-12): усі 5 пройдено.** Стенд: `default` і `tenant_1` у контейнері `tm-pg`, `tenant_2` в окремому контейнері (щоб зупинити лише шард), повільні міграції через `pg_sleep`, Ctrl-C — `kill -INT` групі процесів (як термінал).
1. Два прогони `--tenant`: кожного тенанта провізіонував рівно один, інший після повторної перевірки — `provisioned by another run` / `provisioning in progress elsewhere`; обидва код 0. Другий `--shared` — `shared stage busy` за 0,02 с, код 1.
2. `kill -9` воркера → `Stop: broken_pool`, батько добив другого воркера, `interrupted` зі знімком. `kill -9` батька → воркери загинули одразу (PDEATHSIG), JSON не записано, файл тенанта збережено до обриву. В обох випадках `--release` відмовляв, поки працював `pg_sleep`, потім звільнив; повтор → `RESUMED`, код 0.
3. `--jobs=2`/`--jobs=1` × Ctrl-C 1 → дренаж, `Stop: signal`. × 2 → `hard_stop`: у паралельному режимі оренди лишаються (бекенди працюють); у послідовному psycopg скасував запит, бекенда не лишилось, оренду звільнено (§6.5), знімок `lease free`, файл тенанта — `INTERRUPTED`.
4. `docker stop` шарду 2: тенант у польоті — failed `shard connection lost`, лишився PENDING; решта шарду `not_run`; шард 1 доробив; код 1. Після `docker start` повтор → `RESUMED`.
5. Адмінка → MAINTENANCE: API 503 `tenant_maintenance` + `Retry-After: 600`, `/admin/` доступна; `-s X` без прапорця → skipped, `--tenant` не бере; після `pg_restore` старішого бекапу `--report` показав відставання 1, адмінка → ACTIVE відмовила з підказкою `--maintenance`; `--maintenance` доміграв, адмінка → ACTIVE, дані з бекапу на місці.

**Знахідка.** Після `kill -9` власника, поки його оренда ще жива (TTL 1 год), повторна перевірка `locked` (§9.5, m4) дає ok `provisioning in progress elsewhere` для PENDING-тенанта, і прогін завершується **з кодом 0**, хоча власник мертвий. Тенант лишається PENDING (503) до протухання оренди або `migration_lease --release`. На викатку коду це не впливає: тенант ще не обслуговувався, а `--check` показує його як `WARN stuck PENDING` — лише якщо оренда вільна або протухла; з живою орендою — ні. Рішення за користувачем.

---

## 21. Порядок реалізації

| Крок | Зміст | Залежить від |
|---|---|---|
| 1 | `Status.MAINTENANCE` і `MigrationLease` у наявній міграції `tenants`; `migrator.py`: `owner()`, оренда з M1, `_fresh`, `_open_shard`/`_close_shard`, `pending_plan` з B1, `code_older_than_db`, класифікація. Тести `OwnerTests`, `LeaseTests`, `PendingPlanTests`, `FreshDefaultInAtomic` | — |
| 2 | `migrate_tenant` / `migrate_shared` (M4, Q1, Q3, §6.5). Тести `ProvisionTests`, `UpgradeTests`, частина `MaintenanceTests` | 1 |
| 3 | **Спершу** скопіювати поточний `migrate_schemas.py` у `migrate_schemas_legacy.py` + зміни §15.4 (тест `LegacyCommandTests`). Потім `migrate_schemas` у послідовному режимі: прапорці, етапи, вибір цілей, `pick_next`, повторна перевірка `locked`, підсумок, коди виходу, лог-файли, `--summary-json`. Тести `FlagTests`, `PickNextTests`, `ExitCodeTests`, `RunLogTests`, `SummaryJsonTests` | 2 |
| 4 | Fork-пул, `_worker_init`, сигнали, знімок після аварії. Тест `ParallelE2E` | 3 |
| 5 | `migration_lease`, `reconcile_tenants` (тільки читання) | 1, 2 |
| 6 | Override `migrate` + `TenantsTestRunner`; checks; CI-лінт; `migration_ops` + `LockTimeoutE2E` | 1 |
| 7 | Celery (прямий виклик, retry), консоль, адмінка, middleware (MAINTENANCE, стопер). Тести `CeleryConnectionHygieneTests`, `AdminTests`, `ConsoleStopperTests`, `TasksTests`, решта `MaintenanceTests`. Видалити старий код одразу (автовиправлення reconcile, `CLAIMABLE_STATUSES` тощо) | 2, 5 |
| 8 | Документація (§18) і runbook (§19) | 7 |
| 9 | Docker smoke 1–5 | 4, 7 |

Блокерів на кшталт V1–V6 немає.

---

## 22. Додано під час збирання (погоджено 2026-10-11)

Ці пункти не обговорювалися окремо, їх внесено під час збирання для цілісності тексту. Користувач погодив їх 2026-10-11, тож вони мають ту саму силу, що й рішення §1:
1. **Огородження CAS — `owner = me` без `until > now()`** (§4.4). У чернетці була умова `until > now()`. Вона не давала б фіналізувати законне провізіонування, яке триває довше за TTL. Перехоплення однаково змінює `owner`.
2. **Дрібні виправлення рецензента m1, m2, m3, m4, m5, m7:**
   - `_fresh()` на вході;
   - недоступний `default` зупиняє весь прогін;
   - поле `conn_error` у `Result`;
   - PENDING з живим власником при повторній перевірці → ok;
   - спершу закрити шард, потім звільнити оренду;
   - `active_children()` замість приватного `ex._processes`.
3. **FAILED → NEW в адмінці завжди вимагає галочку `confirm_schema`** з показом стану схеми. Це об'єднує «прийняти порожню схему» з ред. 6 і «прийняти наявну схему» з M4.
4. **MAINTENANCE + схеми немає при `--maintenance` → статус не пишемо:** статус належить оператору.
5. **Після входу в MAINTENANCE — перевірка `migration_lease --list` у runbook.** Це закриває вузьке вікно, коли мігратор захопив оренду між перевіркою в адмінці і CAS.
6. **`MigrationLease` додаємо в наявну міграцію `tenants`,** так само як `choices` для MAINTENANCE.
7. **`code_older_than_db` перевіряється лише при провізіонуванні й реактивації,** як у ред. 6.
8. **Нова перевірка E029** для `TENANT_MIGRATE_LOG_DIR`.

## 23. Відкладено (не в цій редакції)
- **`reconcile_tenants`: одне з'єднання на шард замість одного на тенанта** (ревю 2026-10-12, заміряно 2026-10-13). `pending_plan()` закриває з'єднання шарду (для мігратора — навмисно, через `lock_timeout`), тож `--check` відкриває N з'єднань, `--report` — N+1 (не 2N, як стверджувало ревю). Через RDS Proxy це ~10–50 мс TLS на тенанта. Виправлення: прапорець «не закривати» в `pending_plan()` (лише `set_schema_to_public`) + одне закриття наприкінці `reconcile`, ~10 рядків. Спільний граф міграцій на всіх тенантів — **не** робити (~2,5 мс на тенанта; ризик розійтися з B1 для squashed-міграцій). **Рішення користувача 2026-10-13: поки лишаємо; повернутися, коли `--check` у пайплайні стане помітно повільним.**
- **Автовідновлення застряглого PENDING** (ревю 2026-10-12, дефект 5). Celery `provision_tenant` не повторює `failed conn_error`, задача вмираючого воркера не передоставляється (`acks_late` + `task_reject_on_worker_lost=False`), консоль `provision` приймає лише NEW, адмінка PENDING не змінює. **Рішення користувача 2026-10-13:** лишити оператору (CLI `migrate_schemas -s X`, за потреби `migration_lease --release X`); docstring `tenants/tasks.py` і runbook (`migrations_zero_downtime.md` §13) це описують.
- **Ротація тек логів видаляє теку прогону, що ще триває** (ревю 2026-10-12, дефект 4; `tenants/migrator.py`, `start_run_dir`). Лишаються дві найновіші попередні теки без огляду на те, чи живий їхній прогін: довгий деплой + три короткі ручні запуски на тому ж хості → тека деплою зникає, наступний `_open_log` падає з `FileNotFoundError` (послідовний режим — прогін обривається; паралельний — наступні тенанти failed). Варіанти виправлення: не чіпати теки живих процесів (host+pid у назві), `flock` на теці, або ротація за віком. **Рішення користувача 2026-10-13: повернутися після мерджу і тестування.**
- `--wait-shared=N` (опитування зайнятої оренди `--shared`).
- `--wait-locked`. (`--summary-json` перенесено в цю редакцію, §9.10.)
- CI-тест «FK з моделі `TENANT_APPS` на модель поза `TENANT_APPS`» (автоматична перевірка правила Q3).
- Скрипт відновлення тенанта: він використовуватиме ті самі переходи і перевірки, що й адмінка.
- Beat-задача автовідновлення застряглого PENDING. Зараз це покривають Celery retry і WARN у `--check`.
