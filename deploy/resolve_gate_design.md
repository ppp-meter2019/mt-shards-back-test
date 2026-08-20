# Tenant-resolve gate — дизайн

**Статус:** узгоджений дизайн, до реалізації (під прапором, default off)
**Прапор:** `TENANT_REGISTRY = {"GATE_ENABLED": …, "WARM_ENABLED": …}` (default **off** → поведінка як сьогодні)
**Дата:** 2026-08-14

## Мета

Прибрати з `default` БД:
- **(а)** флуд невідомих хостів (cache-penetration DoS) — кожен унікальний невідомий Host інакше = один `Domain` lookup у спільний пул;
- **(б)** per-request `Domain` lookup для відомих хостів.

При цьому **не ламати коректності** й **не роняти тенантів**, коли Redis/кеш недоступний (fail-open скрізь).

Контекст: тенант-резолв у `tenants.middleware.ShardAwareTenantMiddleware.get_tenant` →
`tenants.resolve_cache` → `CACHES["tenant_resolve"]` (окремий Redis, db2, `volatile-ttl`).
Домени — довільні хости (не лише `*.routegenie.com`), єдине джерело істини — таблиця `Domain`.

---

## Redis-ключі (`tenant_resolve`, db2)

Два РОЗДІЛЬНІ неймспейси: снепшоти йдуть через django_redis під власним піднеймспейсом
`tres:1:host-snap:<host>` (усі записи через `cache._snap_key`), структурні ключі гейта — сирі з
префіксом `treg:*`. SCAN/sweep/`delete_pattern` снепшотів звужені до `host-snap:*` → будь-який
майбутній службовий ключ (інший логічний префікс, напр. `svc:*`, або сирий `treg:*`)
структурно поза зоною sweep і не може бути прийнятий за хост.

| Ключ | Роль | TTL |
|---|---|---|
| `<host>` → snapshot | позитив (`dump` тенант+шард) | **`ttl_by_status`** |
| `<host>` → `NEGATIVE` | кешований промах | 60с |
| `<host>` → `TOMBSTONE` | hold на інвалідації | 5с |
| `treg:hosts` (SET) | усі валідні хости; **існування = флаг консистентності** | немає; **арм 5хв (`EXPIRE NX`)** на add/del/rename |
| `treg:warming` | лок «один writer» | `EX` > тривалості warm |
| `treg:probe:<sec>` | лічильник fill-cap | 2с |

**In-process (на под):** локальний fallback-bucket; single-flight мапа (coalescing).

## `ttl_by_status(status)` — одне правило, у `store` І у `warm`

```
ACTIVE       → None (без TTL)
DEACTIVATED  → 3600с
FAILED       → 1800с
NEW/PENDING  → 120с
```

Обґрунтування: ACTIVE — стійкий стан, тримається теплим через `warm` (без періодичних
expiry-сплесків до `default`). Перехідні/неактивні стани отримують TTL, щоб швидше
самозцілитись, якщо інвалідацію (напр. реактивацію) пропустили.

---

## Потік запиту (однорідний; флаг гейтить ЛИШЕ MISS-гілку)

```
positive GET:
  HIT  → віддати snapshot          # завжди, незалежно від флага
  NEG  → reject (DoesNotExist)     # завжди
  MISS → GATE ↓
```

## GATE (тільки на MISS)

```
pipeline: EXISTS(treg:hosts), SISMEMBER(treg:hosts, host)

  флаг відсутній (EXISTS=0):        # cold / flush / restart / warm-in-progress
      fill_cap.allow()? → резолв БД (coalesced) → store(ttl_by_status) / serve
                        → інакше reject

  member:                           # відомий холодний/новий хост
      резолв БД (coalesced) → store(ttl_by_status)     # без cap (обмежено #hosts + coalescing)

  non-member (флаг present):        # немає в SET → невідомий
      reject (DoesNotExist)         # O(1), БЕЗ БД, БЕЗ cap
```

- `fill_cap` — **лише** на flag-absent гілці; глобальний Redis token-bucket → при збої
  Redis падає на локальний per-pod bucket.
- `coalesced` — single-flight: паралельні запити того самого хоста чекають один резолв.
- `store` у resolve-path — з **`nx=True`** (hold-гонка: повільний резолвер не воскресить
  старе, бо `nx` не спрацьовує на наявному ключі).
- SET пишуть **лише** сигнали (`SADD/SREM`) і `warm` (`RENAME`). Self-heal-probe немає.
- **SADD лише в СТВОРЕНИЙ SET** (guard `EXISTS→SADD` у застосунковому коді, без Lua): інкрементний `add` НЕ може
  воскресити відсутній/прострочений `treg:hosts` як SET з 1 елемента (інакше гейт вважав би його
  авторитетним і hard-reject-ив би всі інші валідні хости). Створює SET **тільки** `reconcile`
  (RENAME). SET відсутній → SADD пропускається, гейт лишається fail-open, `trigger_warm` ребілдить.
  `SREM` безумовний (no-op на відсутньому). Це знімає ампліфікацію «брокер ліг → аутедж усіх тенантів».

Чому non-member = **hard reject**, а не probe: у стійкому стані звичайне створення домену
робить `SADD` одразу (member), а bulk-створення скидає флаг (flag-absent гілка). Тож
«флаг present + валідний хост не в SET» — лише порушення дисципліни; probe там лише
послабив би DoS-захист (дав би атакеру cap-обмежений шлях до БД).

---

## Warm-таска (єдиний force-reconciler)

Під локом `SET treg:warming NX EX <>` (лише один writer):

```
1. redis_alive? інакше вихід (флаг не чіпаємо).
2. dirty = 0
3. db_hosts = set()
   для чанка (2000) у Domain.select_related("tenant__shard").iterator():
       db_hosts |= {d.domain ...}
       SADD treg:hosts:new  *chunk                             # 1 round-trip на чанк
       put_many(chunk)  # group-by-ttl → 1 set_many на кожен distinct TTL (FORCE overwrite)
4. ORPHAN-SWEEP: SCAN позитивів (фільтр службових) → видалити ті, яких немає в db_hosts
5. RENAME treg:hosts:new → treg:hosts        # force-свап (RENAME перезаписує) = флаг present
6. якщо dirty (мутація під час run) → повторити warm     # не загубити останню подію
7. звільнити лок
```

Дія `warm` за станом ключа: **positive / negative / absent / TOMBSTONE → force overwrite**
свіжими даними + `ttl_by_status`. **reconcile НЕ поважає hold** (на відміну від resolve-path):
він авторитетний single-writer, що щойно прочитав БД, тож перезапис hold свіжою датою —
коректний; реальні гонки посеред ребілду ловлять dirty-recheck + orphan-sweep, а не hold.
Запис — **батчами** (`put_many` групує по TTL; SADD пачкою) → ~5 round-trip на 2000 доменів
замість 3×N, тож worst-case reconcile ≪ lock-TTL навіть на тисячах доменів.

Hold тримається на **resolve-path** (`store`/`put` з `nx=True` — повільний резолвер не
воскресить старе); reconcile його свідомо не тримає (див. вище).

## Розклад тасок

- **старт пода:** флаг відсутній → взяти лок → warm; решта подів тим часом fail-open під cap.
- **on-demand:** флаг відсутній (flush/restart) АБО bulk-тригер → warm (дебаунс локом + dirty-recheck).
- **періодично:** **раз на добу** — сітка для забутих тригерів.

---

## Послідовності подій

### A. Холодний старт / порожній Redis
1. Поди обслуговують одразу (жодного per-pod запиту до `default` заради гейта).
2. Запити: HIT→віддати; MISS→flag-absent→fill_cap→резолв (хіти не throttl-яться).
3. Перший под бере лок → warm → позитиви(`ttl_by_status`)+SET → `RENAME` → флаг present.

### B. Стійкий стан
Див. «Потік запиту». Тепло → майже все HIT; флуд невідомих → `SISMEMBER=0` → reject без БД.

### C. Інвалідація (зміна тенанта/домену, хост лишається)
- `forget` → `TOMBSTONE`(5с) на хостах тенанта, позитив стерто. SET **не чіпаємо**.
- Наступний запит: TOMBSTONE→MISS→GATE member→свіжий резолв; `store(nx)` поважає hold.

### D. Domain ADD
1. `forget_host` + `SADD treg:hosts host`.
2. `EXPIRE treg:hosts 300 NX` (арм dead-man switch).
3. тригер warm → перепис + `RENAME` (disarm).
- Хост валідний **миттєво** (SADD видно всім подам).

### E. Domain DELETE
1. `SREM treg:hosts host` + **`delete` позитиву** (не лише tombstone — щоб не віддавати як HIT).
2. `EXPIRE treg:hosts 300 NX`.
3. тригер warm → orphan-sweep + `RENAME`.
- Чутливе видалення = доступ відрізано одразу (крок 1).

### F. Domain RENAME/repoint
- `SREM old + SADD new`; `delete(old)` + `forget(new)`; `EXPIRE treg:hosts 300 NX`; тригер warm.

### G. Bulk / raw / міграції (обхід сигналів) — відповідальність код-шляху
- create/update: **скинути флаг** (`DELETE treg:hosts`) → fail-open → warm додасть/оновить
  + orphan-sweep; АБО прямий тригер warm.
- delete: скинути флаг + (якщо чутливо) явно почистити ключі цих доменів.
- добовий warm — сітка, якщо тригер забули.

### H. Флаг відсутній (flush / restart / провал warm)
- MISS → fail-open під cap; **HIT далі обслуговуються**.
- `treg:hosts` (з 5-хв TTL після мутації) протух → перший под бере лок → warm → флаг back.

---

## Fail-open при мертвому Redis (жоден запит не падає)

| Операція | Клієнт | Redis down → |
|---|---|---|
| positive/neg GET | django-cache (`IGNORE_EXCEPTIONS`) | None → MISS |
| GATE `EXISTS+SISMEMBER` | raw | ловимо → **flag-absent гілка (fail-open)** |
| `fill_cap INCR` | raw | ловимо → **локальний per-pod bucket** |
| `store/forget` | django-cache | no-op |
| `SADD/SREM` | raw | best-effort; warm полікує |
| warm до Redis | raw | флаг не оновлюється → протухне → fail-open |

Мертвий Redis ⇒ кожен запит резолвиться через `default` під **локальним** cap+coalescing
(краще за сьогодні, де Redis-down = pool-timeout). Мертвий `default` (не Redis) →
`OperationalError` → брендований 5xx.

**Окремий баг-фікс (незалежний від прапора):** нормалізація сирого `psycopg.OperationalError`.
django-tenants виконує `SET search_path` на сирому psycopg-курсорі, тож помилка пулу/проксі
(borrow-timeout: `ConnectionException`, підклас `psycopg.OperationalError`) тікає
**НЕ**обгорнутою у `django.db.OperationalError`. Наслідок: `middleware.get_tenant`
хибно класифікує її як «cache path failed» → логує traceback і **ретраїть мертву БД**, а
`process_request` не рендерить брендований `database_error`. Фікс — у `get_tenant`
нормалізувати: `except psycopg.OperationalError as exc: raise OperationalError(str(exc)) from exc`.

---

## Інваріанти

- **`default` майже не чіпаємо:** флуд→`SISMEMBER`(Redis)→reject; зміни доменів→`SADD/SREM`(Redis);
  warm→1 скан/добу; відомі хости→з кеша.
- **Жоден запит не 404-иться через холодний/мертвий Redis:** сумнів → fail-open → БД під cap.
- **Pool захищений завжди:** cap+coalescing у всіх станах.
- **no-TTL ACTIVE безпечний:** orphan-sweep у warm + bulk-тригери + tombstone(`nx`) на оновленні;
  expiry-сплесків немає (ACTIVE не протухає, warm освіжає).
- **Hold тримається на resolve-path** (`store`/`put` з `nx`); reconcile force-перезаписує (батчами).

---

## Структура коду — пакет `tenants/resolver/`

Уся підсистема живе в пакеті `tenants/resolver/` (чіткі межі, HTTP-шар нічого не знає про політику):

- `resolver/flags.py` — **єдине джерело прапорів** `warm_enabled()`/`gate_enabled()` (fail-safe
  gate⇒warm тут); cache і registry делегують сюди. `checks.py` навмисно читає СИРІ налаштування.
- `resolver/service.py` — **фасад** `resolve(hostname, db_resolver, not_found)`: оркестрація
  (кеш → гейт → fill_cap → coalescing → fail-open). Єдина точка входу.
- `resolver/cache.py` — `TenantResolveCache` + `resolve_cache`: снепшот-примітиви
  (`get_snapshot`, `put`(nx)/`store`, `put_many`(batch force), `store_miss`, `forget_*`, `warm`,
  `iter_snapshot_hosts`/`sweep_orphans`, `get_redis_raw_client`). **Один** писар позитивів (`put`).
- `resolver/registry.py` — `HostRegistry` + `host_registry`: `treg:hosts` SET-гейт
  (`check`, `add/remove/arm`, `run_locked` з fenced-локом, `reconcile` + dirty-recheck).
- `resolver/throttle.py` — `fill_cap` (rate-limiter) + `single_flight` (coalescing). NB: НЕ ухвалює
  рішення гейта (це `registry`+`service`) — лише обмежує навантаження на `default`.
- `resolver/markers.py` — `NEGATIVE`/`TOMBSTONE`.
- `resolver/__init__.py` — публічний API (імпортувати звідси, не з підмодулів).

Правки поза пакетом:
- `tenants/middleware.py::get_tenant` — 3 рядки: викликає `resolver.resolve(...)` з db-resolver-closure
  (`_resolve_tenant`, який нормалізує сирий `psycopg.OperationalError`); більше нічого про кеш/гейт не знає.
- `tenants/signals.py` — Domain save/delete: `SADD/SREM` + `arm` + тригер warm (on_commit).
- `tenants/tasks.py` — `reconcile_host_registry_task` (єдина resolve-таска; public-контекст, plain Task).
- команди `warm_resolve_cache` (WARM-aware → reconcile) і `invalidate_resolve_cache` (--all gate-aware).
- `tenants/checks.py` — deploy-time system-check `tenants.E001`: `GATE_ENABLED ⇒ WARM_ENABLED`
  (Error). Fail-safe у коді: `host_registry.gate_enabled = GATE and WARM` → GATE-без-WARM тихо=OFF
  у КОЖНОМУ процесі (безпечно скрізь), а чек робить misconfig гучним на CI/деплої.
  Порядок роллауту: WARM → `warm_resolve_cache` → `bench inspect`/`gate` → GATE.
- `scripts/resolve_cache_bench.py` — режим `gate` («нуль `default`-запитів під `--unique-misses`»).

---

## TODO (окремий тікет)

**Інвалідація кеша на зміну шарда тенанта.** `dump()` кладе поля шарда в snapshot, а
рісивера на `Shard` немає → зміна шарда самозцілюється лише добовим warm (стале поле шарда
→ неправильний роутинг до 24 год). **Не робимо зараз:** зміна шарда — важка операція з
**фізичним переносом даних тенанта між кластерами**; інвалідацію кеша треба вбудувати в той
(окремий) процес переносу, а не в простий `Shard.save()`-рісивер.

*(Решта контуру самодостатня: доки шард тенанта не змінюють, стале поле шарда неможливе.)*
