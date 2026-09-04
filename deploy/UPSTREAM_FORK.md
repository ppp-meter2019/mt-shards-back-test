# Точки форку django-tenants

Цей проєкт **не використовує django-tenants як є**. Він перевизначає вісім місць upstream,
копіює два фрагменти дослівно і спирається на кілька приватних/недокументованих API.
Рішення свідоме — воно було передбачене ще в RGKB Decision Log («team acknowledges
django-tenants may need to be forked»), бо upstream підтримує **одну** БД, а нам треба
schema-per-tenant × shard-per-cluster.

Ціна цього рішення — **бамп `django-tenants` не є бампом версії в `requirements.txt`**. Це
ручна звірка по восьми файлах. Документ існує, щоб ту звірку можна було зробити за годину,
а не за день, і щоб жодна точка не загубилась.

> **Коли читати:** перед підняттям `django-tenants`, `celery` або `Django`.
> **Коли оновлювати:** щойно з'явилась нова точка перевизначення upstream.

---

## 1. Закріплені версії

| Пакет | Пін | Чому саме такий |
|---|---|---|
| `django-tenants` | `==3.10.1` | **точний пін**, не діапазон: усе нижче в цьому файлі прив'язане до внутрішньої будови саме цієї версії |
| `Django` | `==5.2.13` | `Model.save()` має `update_fields` keyword-only (див. §5) |
| `celery[redis]` | `>=5.4,<6` | `DjangoTask` з'явився в 5.4; мажорний cap — через контракт `SchedEntry(dict)` + `**entry` |
| `celery-redbeat` | `>=2.2,<3` | той самий контракт beat-запису |

---

## 2. Мапа точок форку

| # | Наш файл | Що робимо | На що спираємось в upstream |
|---|---|---|---|
| 1 | `tenants/routers.py` | успадковуємо `TenantSyncRouter`; повністю перевизначаємо `db_for_read/write`; **копіюємо тіло** `allow_migrate` | `TenantSyncRouter.app_in_list`, `has_multi_type_tenants`, `get_tenant_types`, `get_public_schema_name` |
| 2 | `tenants/middleware.py` | успадковуємо `TenantMainMiddleware`; перевизначаємо `process_request` (з `super()`) і `get_tenant` | `hostname_from_request` (зрізає `www.`!), `no_tenant_found` → `Http404`, `setup_url_routing`, `TENANT_NOT_FOUND_EXCEPTION` |
| 3 | `tenants/context.py` | **заміна** `schema_context` / `tenant_context` на shard-aware версії | `connection.tenant`, `.set_tenant()`, `.set_schema()`, `.set_schema_to_public()` |
| 4 | `tenants/apps.py:18-23` | monkeypatch `django_tenants.utils.schema_context/tenant_context` | атрибути модуля `django_tenants.utils` |
| 5 | `tenants/management/commands/migrate_schemas.py` | успадковуємо `MigrateSchemasCommand`, переписуємо `handle` | `SyncCommon.handle` (виклик **напряму**), `GET_EXECUTOR_FUNCTION`, `self.options/args/executor/sync_public/sync_tenant/schema_name`, `_notice()`, `parser._actions` |
| 6 | `tenants/management/commands/tenant_command.py` | успадковуємо upstream `Command`, **дзеркалимо** `run_from_argv` | `InteractiveTenantOption.get_tenant_from_options_or_interactive`, розкладка `argv` |
| 7 | `tenants_back/settings_multitenant.py:32-38` (`SHARED_APPS`) | `tenants` **перед** `django_tenants` у `SHARED_APPS` | Django резолвить management-команди за порядком застосунків |
| 8 | `tenants_back/settings_multitenant.py:66-76` (`DATABASE_ROUTERS`) | обхід літеральної перевірки роутера | `django_tenants/apps.py:41-44` |

---

## 3. Дослівно скопійований код — найвищий ризик

Це єдина категорія, яка **зламається тихо**: upstream виправить у себе баг, а наша копія
лишиться зі старою поведінкою.

### 3.1 `tenants/routers.py` → `allow_migrate`

Скопійовано `django_tenants/routers.py:34-58` (гілки multi-type / SHARED vs TENANT +
`app_in_list`). Змінено рівно одне:

```python
# upstream:
if db != get_tenant_database_alias():      # усе, крім 'default', відхиляється
    return False
...
return None                                # «немає думки» → вирішує наступний роутер

# наш:
if connection.schema_name == public_schema_name:
    return db == "default"                 # public — тільки на default
return db != "default"                     # тенантські схеми — тільки на шардах
```

Дві відмінності, які треба тримати в голові:
- upstream завершує `return None` («утримуюсь»), ми — явним `True`/`False`. Тобто наш роутер
  **завжди вирішує**, і `django_tenants.routers.TenantSyncRouter` у списку нижче ніколи не
  отримує керування.
- upstream-перевірка `db != get_tenant_database_alias()` несумісна з multi-DB за побудовою —
  саме заради цього все й почалось.

**Що перевірити на бампі:** `diff` цього блоку з новим `django_tenants/routers.py`.

### 3.2 `tenants/management/commands/tenant_command.py` → `run_from_argv`

Дзеркалить `django_tenants/management/commands/tenant_command.py:18-48` рядок у рядок,
включно з `del argv[1]` і ручним `argparse` для `-s/--schema`. Змінено одне:

```python
# upstream:  connection.set_tenant(tenant); klass.run_from_argv(args)
# наш:       with tenant_context(tenant): klass.run_from_argv(args)
```

Причина: upstream ставить схему лише на з'єднанні `default` і не чіпає `current_db`, тож
обгорнута команда пише в **не той шард**.

**Що перевірити на бампі:** чи не змінилась розкладка `argv` і чи не з'явились нові опції.

---

## 4. Приватні та недокументовані API

Це не публічний контракт upstream — ніхто не зобов'язаний його зберігати.

| Місце | Що чіпаємо | Що станеться, якщо зміниться |
|---|---|---|
| `migrate_schemas.py:102` | `SyncCommon.handle(self, ...)` викликається **напряму**, повз MRO | мовчазна зміна поведінки або `TypeError` |
| `migrate_schemas.py:87-94` | ітерація `parser._actions`, щоб зняти дефолт `--database` | дефолт `'default'` повернеться → повний прогін пропустить усі шарди |
| `migrate_schemas.py` | `self.options`, `self.args`, `self.executor`, `self.sync_public`, `self.sync_tenant`, `self.schema_name`, `self.PUBLIC_SCHEMA_NAME` | атрибути, які `SyncCommon` виставляє як побічний ефект |
| `migrate_schemas.py` | `_notice()` з `SyncCommon` | зникне → `AttributeError` на першому ж виводі |
| `apps.py:18-23` | monkeypatch модуля `django_tenants.utils` | **частковий за природою**: модуль, що імпортував helper ДО `ready()`, лишиться зі старою версією. Тому проєктний код зобов'язаний імпортувати з `tenants.context` — це статично стереже `scripts/ci_guard_context_import.sh` |
| `middleware.py` | успадкований `hostname_from_request` зрізає префікс `www.` і порт | тихо змінить ключ кешу резолву й членство в `treg:hosts` |

---

## 5. Що форком **не** є

**`tenants/celery/*` — це не форк django-tenants.** Ці чотири модулі (`app.py`, `task.py`,
`registry.py`, `compat.py`) переписують ідеї пакета `tenant_schemas_celery`, який **у проєкті
не встановлений** (перевірка: `pip list | grep tenant` → лише `django-tenants`). Тобто
upstream'а, з яким вони могли б розійтись, не існує.

Натомість вони зчеплені з **самим Celery**:

| Файл | Внутрішній API Celery |
|---|---|
| `celery/app.py` | `Celery.registry_cls`, `.task_cls`, `create_task_cls()`, `subclass_with_self()`, перевизначення `send_task()` |
| `celery/task.py` | `celery.contrib.django.task.DjangoTask` (з 5.4), `task.request.headers`, `req.get("_schema_name")`, сигнатура `apply()` |
| `celery/registry.py` | `celery.app.registry.TaskRegistry.register()` |
| `commons/platform/beat.py` | `crontab._orig_minute` та інші `_orig_*` (приватні поля) |

Через це в `requirements.txt` стоїть `celery>=5.4,<6` з мажорним cap'ом.

---

## 6. Відома чистіша альтернатива для §2.8

`settings_multitenant.py:66-76` (`DATABASE_ROUTERS`) зараз перелічує upstream-роутер як no-op, **лише** щоб
задовольнити перевірку:

```python
DATABASE_ROUTERS = [
    "tenants.routers.TenantDatabaseRouter",     # усе вирішує він
    "django_tenants.routers.TenantSyncRouter",  # ніколи не отримує керування
]
```

Але `django_tenants/apps.py:41` читає це так:

```python
tenant_sync_router = getattr(settings, 'TENANT_SYNC_ROUTER', 'django_tenants.routers.TenantSyncRouter')
if tenant_sync_router not in settings.DATABASE_ROUTERS:
    raise ImproperlyConfigured(...)
```

Тобто ім'я роутера **конфігуроване**, і обхід не потрібен:

```python
TENANT_SYNC_ROUTER = "tenants.routers.TenantDatabaseRouter"
DATABASE_ROUTERS   = ["tenants.routers.TenantDatabaseRouter"]
```

`TENANT_SYNC_ROUTER` в upstream 3.10.1 більше ніде не читається (перевірено `grep` по
пакету), тож інших наслідків бути не повинно.

**Статус: не застосовано і не перевірено на живій БД.** Зміна зачіпає завантаження застосунків,
тож робити її варто окремо й з прогоном міграцій, а не разом з іншими правками.

---

## 7. Чекліст бампу `django-tenants`

1. `pip download django-tenants==<нова>` і розпакувати поруч зі старою.
2. `diff` по чотирьох файлах upstream:
   `routers.py`, `middleware/main.py`, `management/commands/__init__.py` (`SyncCommon`),
   `management/commands/migrate_schemas.py`, `management/commands/tenant_command.py`.
3. Звірити скопійовані блоки §3.1 і §3.2.
4. Перевірити, що приватні атрибути з §4 на місці (насамперед `_notice`, `parser._actions`,
   сигнатура `SyncCommon.handle`).
5. `manage.py check` в **обох** режимах + `scripts/ci_mode.sh mt` і `standalone`.
6. `scripts/ci_guard_context_import.sh` — перевіряє, що ніхто не почав імпортувати
   context-хелпери з `django_tenants.utils`.
7. **Обов'язково на живій БД** (DB-free набір цього не покриє):
   `migrate_schemas --shared`, `migrate_schemas --tenant`, `tenant_command <cmd> --schema=X`,
   і запит на тенантський хост — шард має бути правильний.
8. Оновити пін і **цей файл**.

---

## 8. Що вже автоматизовано, а що ні

### Є: канарки на upstream-контракт

`tenants/tests/test_upstream_contract.py` — 6 DB-free тестів, які падають, якщо upstream
прибере те, на що ми спираємось:

| Тест | Стереже |
|---|---|
| `test_synccommon_handle_signature` | виклик `SyncCommon.handle` повз MRO (§4) |
| `test_synccommon_notice_exists` | `_notice()` — весь вивід наших команд (§4) |
| `test_router_app_in_list_exists` | виклик зі скопійованого `allow_migrate` (§3.1) |
| `test_hostname_from_request_still_strips_www` | зрізання `www.` → ключі кешу й `treg:hosts` (§4) |
| `test_database_option_default_is_stripped` | наш патч `parser._actions` (§4) |
| `test_context_monkeypatch_target_and_effect` | monkeypatch у `apps.py:18-23` (§2.4) |

Кожна перевірена ін'єкцією поломки — усі шість реально падають, коли контракт зламано, а не
проходять «за замовчуванням».

### Немає: звірки семантики скопійованого коду

Канарки перевіряють **форму, не поведінку**. Якщо upstream виправить баг **усередині**
`allow_migrate` або `run_from_argv`, усі шість тестів лишаться зеленими, а наші копії — зі
старою логікою. Цю половину ризику не закриває нічого, крім кроків 2-3 чеклісту в §7 (ручний
`diff`). Автоматизувати її можна було б лише зберігши хеш оригінального фрагмента — крихко й
шумно, тому свідомо не робимо.

### Немає: tripwire на саму версію

Тесту «версія досі 3.10.1» немає — бамп не впаде в CI сам по собі, поки не зникне якийсь
символ. Якщо захочете, щоб **будь-яка** зміна версії змушувала пройти чекліст §7, додайте
асерт на `importlib.metadata.version("django-tenants")`.

### Немає: канарок на внутрішні API Celery

`tenants/celery/*` і `commons/platform/beat.py` спираються на `registry_cls`, `task_cls`,
`subclass_with_self()`, `DjangoTask` і приватні `crontab._orig_*` (§5). Нічого з цього не
застережене — цю групу стереже лише мажорний cap `celery<6` у `requirements.txt`.
