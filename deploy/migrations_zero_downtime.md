# Міграції тенантів без простою

Інструкція для тих, хто деплоїть і пише міграції. Дизайн і обґрунтування — у
`deploy/migrations_plan_rev7.md` (далі «план»); тут — що робити і чого не робити.

**Коротко.** Схеми тенантів мігрує лише `migrate_schemas` (і Celery-провізіонування, яке
викликає те саме ядро `tenants/migrator.py`). Кожного тенанта мігрують під його орендою
(рядок у `tenants_migrationlease`), тож два процеси одного тенанта одночасно не чіпають.
Під час оновлення тенант **продовжує працювати** (статус не змінюється, 503 немає), а кожна
DDL-команда має `lock_timeout = 5s`: міграція, яка не може взяти блокування, падає й
повторюється в наступному прогоні, замість того щоб вішати трафік застосунку.

---

## 1. Порядок деплою

```text
CI-джоба міграцій (одна на середовище, concurrency-група):
  1. manage.py migrate_schemas --summary-json=summary.json        # --shared, потім --tenant
       код 0 → далі;  код 1 → СТОП, розбір (§1.2, runbook §13)
  2. manage.py reconcile_tenants --check                          # гейт
       код 0 → далі;  код 1 → СТОП
  3. викатка нового коду (gunicorn, Celery)
```

Міграції йдуть **до** коду: вони мають бути expand-only (§4), тож старий код, що ще
працює, нічого не помічає. Celery-воркери старого коду, які під час викатки візьмуться
провізіонувати тенанта, отримають `code_older_than_db` і повторять задачу через 5 хвилин —
її підхопить уже новий воркер.

### 1.1 Що робить `migrate_schemas`

| Запуск | Що мігрує |
|---|---|
| без прапорців | `--shared`, потім `--tenant`. Невдача `--shared` зупиняє прогін до тенантів |
| `--shared` | `public` на `default`, під орендою 0. Зайнято → одразу failed `shared stage busy` |
| `--tenant [--database X]` | усі NEW, PENDING, ACTIVE, DEACTIVATED на активних недефолтних шардах (лише шард X). FAILED і MAINTENANCE не беруться |
| `-s X [--maintenance]` | один тенант, будь-який статус. FAILED → skipped; MAINTENANCE — лише з `--maintenance` |

Прапорці прогону: `--jobs N`, `--jobs-per-shard N` (§10), `--keep-going`,
`--max-failures N` (типово 3; лише з `--keep-going`, інакше попередження «ignored»),
`--summary-json PATH` (§10.2).

Відмова ще до старту (код 1), щоб команда не «вдавала успіх», роблячи менше, ніж просили:
`-s public --tenant` (використайте `--shared`); `--tenant --database` з шардом `default`,
з **неактивним** шардом (його тенантів — поштучно через `-s <schema>`) або з alias, для
якого немає запису `Shard`.

### 1.2 Підсумок і коди виходу

Наприкінці — рядок на кожну ціль (`ok`, `noop`, `failed`, `skipped`, `not_run`,
`interrupted`), лічильники і `Logs: <тека прогону>`. Якщо прогін зупинено — рядок
`Stop: <причина>`:

| `Stop:` | Що сталося |
|---|---|
| `fail_fast` | перша невдача без `--keep-going`; решта — `not_run` |
| `max_failures:<шард>` | з `--keep-going`: N невдач поспіль на одному шарді |
| `shared_failed` | невдача `--shared`; тенантів не чіпали |
| `default_unreachable` | недоступна база `default` |
| `signal` | перший Ctrl-C / SIGTERM: дренаж (§9) |
| `hard_stop` | другий сигнал (§9) |
| `broken_pool` | паралельний воркер помер (наприклад, OOM killer) |

- **Код 0** — усі цілі `ok`/`noop` і прогін не зупинявся. Тенант, пропущений як `locked`,
  наприкінці перевіряється ще раз: якщо його домігрував інший процес, він стає `ok`.
- **Код 1** — усе інше. **Код 2** — помилка аргументів.
- Недоступний шард: решта тенантів цього шарду — `not_run (shard unreachable)`, інші
  шарди продовжують; код 1.

---

## 2. Вимога: одна CI-джоба міграцій за раз

Пайплайн міграцій **мусить** мати concurrency-групу (одна джоба на середовище; GitLab
`resource_group`, GitHub Actions `concurrency`). Оренди захищають від того, щоб два прогони
мігрували одного тенанта, але два прогони паралельно — це вдвічі більше з'єднань і `locked`
у звіті. Другий одночасний `--shared` одразу падає з `shared stage busy: held by …`.

## 3. Вимога: тека логів

Кожен запуск `migrate_schemas` **з командного рядка** пише окремий файл на кожну ціль:

```text
<TENANT_MIGRATE_LOG_DIR>/                    (типово tenants_back/logs/migrations)
  2026-10-12T12-00-03Z_app-1_4312/           ← один прогін: час старту UTC, хост, pid
    _shared.log                              ← етап --shared
    acme.log                                 ← тенант acme
    _summary.log                             ← той самий підсумок, що на екрані
```

- У файлі тенанта: шапка (схема, шард, статус на старті, owner оренди, час), **повний**
  вивід міграцій, повний traceback при помилці, фінальний рядок з результатом і
  INVALID-індексами. На екран traceback не потрапляє — лише рядок результату.
- Зберігаються **дві попередні** теки прогонів плюс поточна; старіші видаляються на старті.
- **Одноразовий контейнер:** `TENANT_MIGRATE_LOG_DIR` має бути змонтованим томом (або теку
  збирає лог-колектор), інакше логи зникнуть разом з контейнером. Значення задається в
  `settings_local_multitenant.py`; прапорця командного рядка немає.
- `call_command("migrate_schemas", …)` (Celery, `bootstrap_tenant`, тести) файлів не пише.

---

## 4. Правила для міграцій: expand-only

Новий код і старий код працюють з однією схемою одночасно (під час викатки, і поки
тенанти домігровуються). Тому кожна міграція має бути сумісною з **попереднім** релізом:

- нове поле — `null=True` або `db_default`; ніколи «NOT NULL без дефолту»;
- **без перейменувань** полів і таблиць (це «видалити + додати» для старого коду);
- видалення поля — **у два релізи**: спершу код перестає його читати
  (`SeparateDatabaseAndState`, лише стан), у наступному релізі — `RemoveField`;
- FK створюється **без індексу** (`db_index=False`), індекс — окремою CIC-міграцією (§6);
- constraint — `NOT VALID`, потім окремою міграцією `VALIDATE CONSTRAINT`;
- **без `AlterField` зі зміною типу** — нова колонка + бекфіл + перемикання;
- бекфіли — окремо від DDL, батчами, без довгих транзакцій;
- міграції **одного тенанта** не довші за TTL оренди (1 година): інакше її може перехопити інший прогін, поки власник між двома міграціями;
- застосовані міграції **не редагуються**;
- фіча-флаг не захищає від ORM: модель з новим полем читає його в кожному `SELECT`.

## 5. Ще два правила

- **Немає міграцій, що одночасно чіпають схему тенанта і `public`** (Q3). Тенантські
  міграції виконуються в схемі тенанта; міграція, якій потрібна таблиця `public`, ламає
  провізіонування під `lock_timeout` і повтор оновлення.
- **Міграції та `post_migrate`-обробники не закривають і не перевідкривають з'єднання з
  БД.** Нове з'єднання працювало б без `lock_timeout` мігратора.

---

## 6. `lock_timeout`

| | Оренда | `lock_timeout` |
|---|---|---|
| Захищає від | іншого мігратора | трафіку застосунку (веб, Celery, звіти) |
| Де | таблиця `tenants_migrationlease` | PostgreSQL, кожна DDL-команда |
| Чекає? | ні | до 5 с (`TENANT_MIGRATE_DDL_LOCK_TIMEOUT`) |
| Якщо не вдалося | skipped `locked` | помилка 55P03 → failed `lock_timeout` |

- Ставиться **лише** на з'єднання мігратора з шардом, на час одного тенанта
  (`set_config`, `application_name = tm:<pk>`). Веб, Celery-задачі, DBA і ролі БД його не
  мають; після тенанта з'єднання закривається (§7 плану).
- **55P03 при оновленні:** статус тенанта не змінюється, транзакцію міграції відкочено;
  наступний прогін повторить саме цю міграцію. Причина — довга транзакція на таблиці
  тенанта: подивіться `pg_stat_activity` на шарді й повторіть.
- **55P03 при провізіонуванні:** тенант → FAILED. На новій порожній схемі цього бути не
  може; якщо сталося — порушено правило §5 (міграція чіпає `public`).
- Навіщо це: `ALTER TABLE`, який чекає на `ACCESS EXCLUSIVE`, ставить у чергу за собою
  **всі** наступні `SELECT` на цій таблиці. Без таймауту одна довга транзакція звіту
  перетворюється на простій тенанта.

## 7. `CREATE INDEX CONCURRENTLY` і нетранзакційні міграції

CIC за визначенням чекає на всі старші транзакції, тож з `lock_timeout = 5s` він упаде.
Рецепт (`commons/platform/migration_ops.py`, працює і в standalone):

```python
from commons.platform.migration_ops import LockTimeoutOff, LockTimeoutRestore

class Migration(migrations.Migration):
    atomic = False
    operations = [
        LockTimeoutOff(),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AddIndex("model", models.Index(
                fields=["f"], name="app_model_f_0042_idx"))],
            database_operations=[
                migrations.RunSQL('DROP INDEX CONCURRENTLY IF EXISTS "app_model_f_0042_idx"',
                                  migrations.RunSQL.noop),
                migrations.RunSQL('CREATE INDEX CONCURRENTLY "app_model_f_0042_idx" ON "app_model" ("f")',
                                  'DROP INDEX CONCURRENTLY IF EXISTS "app_model_f_0042_idx"'),
            ]),
        LockTimeoutRestore(),
    ]
```

- Кожна міграція з `atomic = False` починається з `LockTimeoutOff()` і закінчується
  `LockTimeoutRestore()`; унікальне ім'я індексу в кожній міграції.
- **`CONCURRENTLY IF NOT EXISTS` і `AddIndexConcurrently` заборонені:** невдалий CIC лишає
  INVALID-індекс, який `IF NOT EXISTS` вважав би наявним — назавжди. `DROP … IF EXISTS`
  перед створенням робить повтор безпечним.
- CI-лінт `commons/tests/test_migration_lint.py` перевіряє: `LockTimeoutOff`/`LockTimeoutRestore` у кожній міграції з `atomic = False` (і `atomic: bool = False`); перед кожним `CREATE [UNIQUE] INDEX CONCURRENTLY "<ім'я>"` — `DROP INDEX CONCURRENTLY IF EXISTS "<ім'я>"` у тій самій міграції; відсутність `CONCURRENTLY IF NOT EXISTS` і `AddIndexConcurrently`. **Не** перевіряє (відповідальність автора): унікальність імені індексу між міграціями і SQL, зібраний під час виконання (f-рядки, змінні).
- **INVALID-індекс** після невдалого прогону видно в рядку результату, у файлі тенанта і в
  `reconcile_tenants --report`. Повтор міграції його перебудує; вручну —
  `DROP INDEX CONCURRENTLY "<schema>"."<idx>"`. Живий CIC теж виглядає як INVALID —
  перевірте оренду й сесію `tm:<pk>` (`migration_lease --list`).

---

## 8. Статуси, які бачить оператор

| Статус | Веб | Хто змінює |
|---|---|---|
| NEW, PENDING | 503 `tenant_not_ready` | мігратор (провізіонування) |
| ACTIVE | працює | мігратор лише при `schema_missing`; адмінка/консоль |
| DEACTIVATED | 403 | адмінка, консоль |
| FAILED | 503, власна `/admin/` доступна | мігратор; вихід — адмінка FAILED → NEW з галочкою |
| MAINTENANCE | 503 `tenant_maintenance`, `Retry-After: 600`, `/admin/` доступна | **лише** адмінка |

**Повернення в ACTIVE** (з DEACTIVATED чи MAINTENANCE) проходить через стопер: схема має
існувати й мати всі міграції цього коду. Інакше адмінка показує помилку з командою
(`migrate_schemas -s X`, для MAINTENANCE — `-s X --maintenance`), а консоль відповідає 409
`schema_behind` / `schema_missing` або 503 `deploy_in_progress` / `shard_unreachable`.

**Карантин через DEACTIVATED.** Тенант, який не мігрується, а реліз потрібен: deactivate →
`reconcile_tenants --check` (DEACTIVATED не перевіряється) → викатка → виправлення →
`migrate_schemas -s X` → activate (стопер пропустить лише змігрованого).

**MAINTENANCE** — для ручних робіт зі схемою (відновлення з бекапу). Мігратор і Celery такого
тенанта не беруть, деплой через нього не червоніє, консоль нічого з ним не робить (409,
включно з `DELETE`). Сценарій — runbook, рядок «Відновлення тенанта з бекапу».

---

## 9. Переривання

Лише для запуску з командного рядка; `call_command` сигналів не перехоплює.

- **Перший Ctrl-C / SIGTERM — дренаж:** нових тенантів не беремо, ті, що мігрують,
  доробляються. `Stop: signal`, код 1.
- **Другий — жорстка зупинка:** у паралельному режимі воркери отримують SIGKILL, у
  послідовному — переривання поточного запиту. `Stop: hard_stop`, обірвані цілі —
  `interrupted`. Для кожної підсумок друкує **знімок з БД**: статус, скільки міграцій
  відстає (або «схеми немає»), хто тримає оренду, і підказку
  `migration_lease --release <schema>`.
- **Оренда після обриву.** Після звичайного завершення чи помилки оренду звільняємо завжди.
  Після Ctrl-C — лише якщо запит на сервері вже завершився. Після SIGKILL оренда лишається
  до свого TTL (1 година) або до `migration_lease --release`. Поки процес PostgreSQL
  міграції ще працює, інший мігратор оренду не візьме — ні протухлу, ні вільну (після
  обриву з'єднання оренду звільнено, а сервер міграцію ще доробляє): тенант пропускається
  як `locked (… backend still active: pid …)`.

```bash
manage.py migration_lease --list                 # усі зайняті оренди + чи працює ще бекенд tm:<key>
manage.py migration_lease --release acme         # лише ПРОТУХЛУ оренду і лише коли бекенда міграції немає
manage.py migration_lease --release shared       # оренда етапу --shared
manage.py migration_lease --release key=42       # оренда видаленого тенанта (форма з --list)
manage.py migration_lease --release acme --force # без перевірок — лише коли процес-власник точно мертвий і бекенда немає
```

---

## 10. Паралельність і бюджет з'єднань

- `--jobs N` — скільки тенантів одночасно; `--jobs-per-shard M` — з них на одному шарді
  (`M ≤ N`). Типово 1/1 (`TENANT_MIGRATE_JOBS`, `TENANT_MIGRATE_JOBS_PER_SHARD`).
- Паралельний режим — **лише з командного рядка**, коли `N > 1` і цілей більше однієї:
  fork-пул; воркер мігрує одного тенанта за раз, ігнорує Ctrl-C і гине разом з батьком.
- **Бюджет з'єднань:** на шард — до `M` міграційних з'єднань плюс короткі перевірки; до
  `default` — до `N` коротких (закриваються після кожного звернення). Рахуйте це поверх
  того, що тримає веб. Якщо веб ловить нестачу з'єднань — дренаж (Ctrl-C) і менші `--jobs`.
- Порядок: шарди по колу, у межах шарду — FIFO; лічильник `--max-failures` іде в порядку
  завершення.

### 10.1 Пулер з'єднань

Потрібна **сесійна** семантика: django-tenants перемикає схему сесійним `SET search_path`,
мігратор ставить `set_config(…, false)`. RDS Proxy (обов'язковий) такі сесії пінить — це
підходить. PgBouncer — лише session mode. Міграція, яка довго рахує в Python без запитів,
може впертися в idle-timeout пулера — бекфіли робіть окремо, батчами. Міграція одного
тенанта, довша за 24 години (максимальне життя клієнтського з'єднання RDS Proxy), не
підтримується. Деталі — `deploy/DATABASE_SETUP.md` §9.

### 10.2 `--summary-json=PATH` для CI

Той самий підсумок у JSON — щоб не розбирати текст. Пишеться **атомарно** (`PATH.tmp` →
`os.replace`) один раз наприкінці, навіть при коді 1, дренажі чи жорсткій зупинці; не
пишеться при помилці аргументів і при SIGKILL самого `migrate_schemas`. `-` (stdout)
заборонено. Тека має існувати.

```json
{"version": 1, "exit_code": 1, "stop": {"reason": "max_failures", "shard": "tenant_1"},
 "counts": {"ok": 40, "noop": 112, "failed": 3, "skipped": 1, "not_run": 20, "interrupted": 0},
 "log_dir": "/srv/app/logs/migrations/2026-10-12T12-00-03Z_app-1_4312",
 "results": [{"stage": "tenant", "schema": "acme", "shard": "tenant_1", "kind": "failed",
              "reason": "lock_timeout", "msg": "…", "log_file": "acme.log", "…": "…"}]}
```

- Скрипти спираються на `exit_code`, `stop.reason` і `results[].reason` (фіксований список
  кодів, план §9.10); `msg` — для людей.
- Зберігайте файл як **артефакт CI-джоби** разом з текою прогону.

```bash
manage.py migrate_schemas --summary-json=summary.json || true
jq -r '.results[] | select(.kind != "ok" and .kind != "noop") | "\(.schema) \(.kind) \(.reason)"' summary.json
test "$(jq .exit_code summary.json)" -eq 0
```

---

## 11. Заборонено

- **`manage.py migrate`** у multi-tenant режимі — відмова з підказкою; працює лише всередині
  створення тестових баз (`tenants.test_runner.TenantsTestRunner`). `testserver` не
  підтримується. `tenant_command migrate` теж відмовить.
- `migrate_schemas --executor` (крім `standard`), `--check`, `--list` (→
  `reconcile_tenants --check`), `--plan` (→ `reconcile_tenants --report --plan`).
- `TENANT_MULTIPROCESSING_*` у налаштуваннях і змінна оточення `EXECUTOR` — system checks
  `tenants.E020` / `tenants.W021`.

### 11.1 `migrate_schemas_legacy` — тимчасовий запасний варіант

Дослівна копія старої команди. **Лише вручну**, коли новий мігратор поводиться неочікувано,
а реліз потрібен. Видалення — окремим рішенням після кількох релізів на новому міграторі.

**Перед запуском обов'язково** `migration_lease --list` — має бути порожньо: стара команда
оренду не бере й чужої не бачить, інших захистів від паралельної роботи немає.

| | `migrate_schemas` | `migrate_schemas_legacy` |
|---|---|---|
| Оновлення ACTIVE/DEACTIVATED | статус не змінюється, тенант працює | тенант у PENDING → **веб віддає 503** до кінця його міграції |
| Помилка оновлення | статус не змінюється | FAILED (потрібне FAILED → NEW в адмінці) |
| Мютекс | оренда + огороджений CAS | лише CAS статусу |
| `lock_timeout` | 5 с | немає: DDL може чекати блокування необмежено |
| Перша помилка | `--keep-going` / `--max-failures` | зупинка прогону |
| Паралельність | власний fork-пул | upstream `--executor=multiprocessing` |
| MAINTENANCE / FAILED | за правилами §8 | пропускає з оманливим «already claimed by another process» |

---

## 12. Перевірка стану

```bash
manage.py reconcile_tenants --check                  # гейт: public і всі ACTIVE актуальні? (код 0/1)
manage.py reconcile_tenants --report                 # таблиця всіх тенантів, public першим
manage.py reconcile_tenants --report -s acme --plan  # один тенант + імена незастосованих міграцій
manage.py reconcile_tenants --report --only-pending
```

`reconcile_tenants` **нічого не пише**. `--check` перевіряє ACTIVE і на неактивних шардах
(яких `--tenant` не бере) — відсталий тенант там валить гейт з підказкою `migrate_schemas -s X`.
Застряглий PENDING — лише `WARN`. Колонка NEXT у `--report` підказує наступну команду.

---

## 13. Runbook

| Ситуація | Дія |
|---|---|
| Код 0 після `migrate_schemas` | `reconcile_tenants --check`; код 0 → викатка |
| Код 1, failed `migration_error` | файл тенанта в теці прогону (`Logs:`) → виправити й накотити вперед (fix-forward) → `migrate_schemas -s X` або `--tenant --keep-going` |
| Код 1, failed `lock_timeout` | довгі транзакції в `pg_stat_activity` на шарді → повтор |
| Код 1, skipped `locked` | `migration_lease --list`; власник живий → дочекатися; мертвий → `--release` → повтор |
| `shared stage busy` | паралельний деплой або ручний запуск → дочекатися; мертвий прогін → `migration_lease --release shared` |
| Тенант не мігрується, а реліз потрібен | карантин: deactivate → `--check` → викатка → виправлення → `migrate_schemas -s X` → activate |
| Застряглий PENDING (`WARN` у `--check`; Celery-провізіонування впало з `conn_error`, воркер помер, вичерпано retry) | **Лише з бекенд-хоста**, консоль/адмінка PENDING не змінюють: `reconcile_tenants --report -s X` → якщо оренда висить і `migration_lease --list` показує SESSION `none` → `migration_lease --release X` (оренда ще не протухла → `--force`, лише якщо процес-власник точно мертвий) → `migrate_schemas -s X` (→ `provisioned (RESUMED)`). Автовідновлення немає (план §23) |
| Оренда після SIGKILL | SESSION `none` у `--list` → оренда ще жива (до години): `migration_lease --release X --force`, але лише переконавшись, що процес-власник мертвий (`ps -o lstart= -p <pid>` на його хості — owner = `host:pid:start`); живий власник між запитами виглядає так само. Протухлу оренду звільняє `--release X` без `--force` |
| INVALID-індекс | `DROP INDEX CONCURRENTLY "<schema>"."<idx>"` → повтор |
| FAILED `schema_missing` | відновити зі snapshot/PITR (через MAINTENANCE, рядок нижче) або адмінка FAILED → NEW з галочкою |
| FAILED `schema_exists` | перевірити, чия це схема. Сирітська → `drop_tenant_schema`, потім адмінка FAILED → NEW. Належить тенанту → адмінка FAILED → NEW з галочкою |
| FAILED після виправлення причини | адмінка FAILED → NEW з галочкою `confirm_schema` → `migrate_schemas -s X` (або кнопка provision у консолі) |
| **Відновлення тенанта з бекапу** | адмінка → MAINTENANCE → `migration_lease --list`: немає оренди й активної сесії для тенанта → DBA `drop`/`create` схеми + `pg_restore` → `migrate_schemas -s X --maintenance` → `reconcile_tenants --report -s X` → адмінка → ACTIVE |
| Жорстка зупинка | знімок у підсумку → `reconcile_tenants --report` → `migration_lease --list` → повтор |
| Веб ловить нестачу з'єднань під час прогону | дренаж (Ctrl-C), зменшити `--jobs` / `--jobs-per-shard` |
| Прогін стоїть на одному тенанті й не рухається | на `default` і на шарді: `SELECT pid, now()-xact_start, wait_event_type, left(query,80) FROM pg_stat_activity WHERE wait_event_type = 'Lock'` → знайти, хто тримає блокування (`pg_blocking_pids(pid)`). Мігратор на `default` не має `lock_timeout`: **не тримати відкритих транзакцій на `tenants_tenant` / `tenants_migrationlease`** (ручний `BEGIN` у psql). «Тихо мертве» з'єднання саме дасть помилку за ~1 хв (TCP keepalive) |
| Celery `provision_tenant` skipped `locked` | нічого: retry через 5 хв, до 15 разів |
| Новий мігратор поводиться неочікувано, реліз потрібен | **обов'язково** `migration_lease --list` (має бути порожньо) → `migrate_schemas_legacy --tenant` (або `-s X`). Пам'ятати: кожен тенант на час своєї міграції віддає 503, помилка → FAILED (§11.1) |
