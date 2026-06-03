# Деплой `tenants_back` за nginx + gunicorn

Покрокова інструкція для прод-розгортання. Замініть `example.com` на ваш
реальний домен і `ubuntu` — на потрібного користувача.

Артефакти, що поряд:

| Файл                                                          | Призначення                                                  |
|---------------------------------------------------------------|--------------------------------------------------------------|
| `nginx.example.conf`                                          | віртуальний хост (apex + wildcard сабдомени)                 |
| **`../bin/gunicorn_start.sh`**                                | bash-launcher для supervisor — основний шлях                 |
| **`../tenants_back/settings_local.py.example`**               | шаблон прод-overrides — копіюємо як `settings_local.py`      |
| `gunicorn.conf.py`                                            | конфіг gunicorn у Python-формі — для systemd-варіанту        |
| `gunicorn.service`                                            | альтернатива через systemd                                   |

---

## 1. Встановити gunicorn

```bash
cd /home/ubuntu/tenants_back
source venv/bin/activate
pip install gunicorn   # або додати у requirements.txt
```

---

## 2. Структура каталогів

```
/home/ubuntu/tenants_back/                  # код (git checkout цієї теки)
/home/ubuntu/tenants_back/venv/             # virtualenv
/home/ubuntu/tenants_back/staticfiles/      # collectstatic → сюди
/home/ubuntu/tenants_back/run/gunicorn.sock # UNIX-сокет (створюється скриптом)
/home/ubuntu/tenants_back/bin/gunicorn_start.sh
/home/ubuntu/tenants_front/                 # фронт (vanilla JS)
```

Користувачі/групи (`ubuntu` уже існує на стандартному AMI — створювати не треба):

```bash
# Тека для сокета: власник ubuntu, група www-data, setgid + group-traversable,
# щоб nginx (www-data) міг зайти в неї до сокета.
sudo install -d -o ubuntu -g www-data -m 2750 /home/ubuntu/tenants_back/run
```

Привілеї скидає **сам gunicorn**: supervisor запускає `bin/gunicorn_start.sh`
**від root**, а gunicorn через `--user=ubuntu --group=www-data` стає
`ubuntu:www-data` і `chown`-ить сокет у `ubuntu:www-data`. Це принципово: якби
скрипт стартував уже від `ubuntu` (non-root), `setgid(www-data)` був би
заборонений — членства в додатковій групі для `setgid` недостатньо. nginx
(`www-data`) читає сокет за груповим доступом.

---

## 3. Основний шлях — supervisor + `bin/gunicorn_start.sh`

### 3.1 Прод-значення живуть у `settings_local.py`

`bin/gunicorn_start.sh` сам по собі **не містить** ні `SECRET_KEY`, ні
`ALLOWED_HOSTS`, ні DB-кредів. Усе це йде в окремий Python-файл, який
підвантажується наприкінці `tenants_back/settings.py`:

```python
try:
    from .settings_local import *
except ImportError:
    print("Can't load local settings!")
```

Перед першим запуском скопіюй шаблон і відредагуй:

```bash
cd /home/ubuntu/tenants_back
cp tenants_back/settings_local.py.example tenants_back/settings_local.py
nano tenants_back/settings_local.py           # SECRET_KEY, домен, DB_PASSWORD
```

`settings_local.py` обов'язково додай у `.gitignore`:
```
tenants_back/tenants_back/settings_local.py
```

Прапори gunicorn у самому `exec` (див. `bin/gunicorn_start.sh`):

| Прапор                                          | Що робить                                                       |
|-------------------------------------------------|-----------------------------------------------------------------|
| `--worker-class uvicorn.workers.UvicornWorker`  | ASGI-воркер (async) — дефолт проєкту                            |
| `--bind=unix:$SOCKFILE`                         | UNIX-сокет, що матчиться з nginx `upstream`                      |
| `--user=$USER --group=$GROUP`                   | drop privileges → `ubuntu:www-data` (gunicorn стартує від root)  |
| `--workers=$NUM_WORKERS`                        | к-сть процесів (орієнтир `2*CPU + 1`)                           |
| `--timeout=120`                                 | таймаут воркера                                                 |
| `--max-requests` / `--max-requests-jitter`      | періодичний перезапуск воркерів (захист від витоків памʼяті)    |
| `--access-logfile=-` / `--error-logfile=-`      | логи в stdout/stderr → supervisor ротейтить                     |

### 3.2 Sanity-check «руками»

```bash
sudo /home/ubuntu/tenants_back/bin/gunicorn_start.sh   # від root: gunicorn сам скине привілеї до ubuntu:www-data
# В іншій консолі:
curl --unix-socket /home/ubuntu/tenants_back/run/gunicorn.sock \
     http://example.com/api/ -i
```

Якщо відповідає DRF (нехай навіть 401 Unauthorized) — підключаємо
supervisor.

### 3.3 Конфіг supervisor

```ini
; /etc/supervisor/conf.d/tenants_back.conf
[program:tenants_back]
command=/home/ubuntu/tenants_back/bin/gunicorn_start.sh
; НЕ ставимо user= — supervisor стартує від root, а gunicorn сам скидає
; привілеї до ubuntu:www-data (інакше setgid(www-data) заборонений).
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/supervisor/tenants_back.log
stdout_logfile_maxbytes=20MB
stdout_logfile_backups=5
stopsignal=TERM
stopasgroup=true
killasgroup=true
```

```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status tenants_back
sudo tail -f /var/log/supervisor/tenants_back.log
```

### 3.4 Робочий flow апдейтів

```bash
cd /home/ubuntu/tenants_back
sudo -u ubuntu git pull
sudo -u ubuntu venv/bin/pip install -r requirements.txt
sudo -u ubuntu venv/bin/python manage.py migrate_schemas --shared --database=default
sudo -u ubuntu venv/bin/python manage.py migrate_schemas --tenant
sudo -u ubuntu venv/bin/python manage.py collectstatic --noinput
sudo supervisorctl restart tenants_back
```

---

## 4. nginx

Шаблон — `nginx.example.conf`. Підставити свій домен і шляхи до сертифікатів,
скопіювати в `/etc/nginx/sites-available/tenants.conf`, увімкнути:

```bash
sudo ln -s /etc/nginx/sites-available/tenants.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
sudo ufw allow 8000/tcp     # відкрити публічний API-порт
```

Архітектура — **два `server`-блоки з одним wildcard-сертифікатом**:

| Порт    | Що віддає                                              | `location` блоки                                                                  |
|---------|--------------------------------------------------------|------------------------------------------------------------------------------------|
| `:443`  | Тільки SPA-фронт                                       | `/` → SPA (`try_files $uri /index.html =404`)                                      |
| `:8000` | Публічний Django: API + admin + статика для адмінки    | `/api/` → gunicorn, `/admin/` → gunicorn, `/static/` → alias, `/` → 404            |

`:443` Django **не торкається** — Django (включно з адмінкою) живе
**виключно** на `:8000`. `:8000` проксує у gunicorn-сокет
`unix:/home/ubuntu/tenants_back/run/gunicorn.sock` із обов'язковим
`proxy_set_header Host $host;` — `TenantMainMiddleware` маршрутизує
саме за `Host` (порт у виборі схеми участі не бере).

Що це дає:
- Фронт із `https://alpha.example.com` стукається на
  `https://alpha.example.com:8000/api/...` (cross-origin, тому в Django
  активний `CORS_ALLOWED_ORIGIN_REGEXES` для всіх `*.example.com`).
- Зовнішні клієнти (curl, мобілка, чужі бекенди) ходять на той самий
  `https://<тенант>.example.com:8000/api/...`.
- Django admin доступний на `https://<тенант>.example.com:8000/admin/`.
  Для tenant-адміна — `https://example.com:8000/admin/` (apex без сабдомена).
  CSRF для цього порту покритий — у `settings_local.py`
  `CSRF_TRUSTED_ORIGINS` має `https://*.example.com:8000`.
- Сайт-фронт чистіший: 443-ій порт не проксує **жодного** Django-шляху,
  не «знає» про `/admin/`, нічого зайвого не експонує.

Якщо хочеш усе на 443 без окремого API-порта — прибери `server { listen 8000 ssl; ... }`
блок із конфігу і постав у `tenants_front/config.js` `API_PORT = ""`.

---

## 5. TLS

Wildcard-сертифікат від Let's Encrypt через DNS-01:

```bash
sudo certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d example.com -d '*.example.com'
```

(Замінити `--dns-cloudflare` на ваш DNS-провайдер; HTTP-01 для wildcard
не підходить.)

---

## 6. Альтернатива — systemd замість supervisor

Якщо supervisor не використовуєте, є готовий юніт `gunicorn.service` +
`gunicorn.conf.py`. Налаштування Django (`SECRET_KEY`, `ALLOWED_HOSTS`,
DB, CORS) і тут живуть у `tenants_back/settings_local.py` — однаково з
supervisor-варіантом.

```bash
sudo cp /home/ubuntu/tenants_back/deploy/gunicorn.service /etc/systemd/system/tenants_back.service
sudo systemctl daemon-reload
sudo systemctl enable --now tenants_back
```

`gunicorn.service` тримає `RuntimeDirectory=tenants_back` і bіnd на
`/run/tenants_back.sock` — у цьому варіанті оновіть `nginx.example.conf`:

```nginx
upstream tenants_back {
    server unix:/run/tenants_back.sock fail_timeout=0;
}
```

Робочий flow апдейтів — той самий, тільки в кінці:

```bash
sudo systemctl reload tenants_back   # graceful HUP
```

---

## 7. Перший bootstrap на чистому сервері

```bash
cd /home/ubuntu/tenants_back
sudo -u ubuntu venv/bin/python manage.py migrate_schemas --shared --database=default
sudo -u ubuntu venv/bin/python manage.py sync_shards --activate
sudo -u ubuntu venv/bin/python manage.py collectstatic --noinput

# public-тенант (на default shard) + tenant-admin:
sudo -u ubuntu venv/bin/python manage.py bootstrap_public \
  --domain example.com --username root --password 'STRONG'

# бізнес-тенант: bootstrap_tenant сам викликає migrate_schemas (створює схему
# на вказаному shard, NEW → ACTIVE) і заводить company-admin:
sudo -u ubuntu venv/bin/python manage.py bootstrap_tenant \
  --schema alpha --name "Alpha" --domain alpha.example.com \
  --shard tenant_1 \
  --admin-username admin --admin-password 'STRONG'
```

---

## 8. Часті граблі

| Симптом                                     | Причина                                     | Як виправити                                                                                  |
|---------------------------------------------|---------------------------------------------|-----------------------------------------------------------------------------------------------|
| nginx → `502 Bad Gateway`                   | gunicorn стартував від `ubuntu`, не від root | supervisor БЕЗ `user=` (стартує від root); у `gunicorn_start.sh` стоять `--user=ubuntu --group=www-data` |
| `Permission denied` на сокеті при старті    | теки `run/` нема або не та власність         | `install -d -o ubuntu -g www-data -m 2750 /home/ubuntu/tenants_back/run`                       |
| `DisallowedHost` у логах                    | `ALLOWED_HOSTS` без потрібного host'а         | у `settings_local.py` додати `.example.com` (з крапкою → wildcard)                            |
| Свіжий код — старі воркери                  | gunicorn форкнувся при старті                | `sudo supervisorctl restart tenants_back` (для systemd: `systemctl reload`)                   |
| `/static/` 404                              | забули `collectstatic`                       | `manage.py collectstatic --noinput`                                                            |
| Admin: `CSRF verification failed`           | за TLS-проксі, Django про це не знає         | у `settings_local.py`: `SECURE_PROXY_SSL_HEADER` + `CSRF_TRUSTED_ORIGINS` (з `:8000`)         |
| `502` лише на сабдоменах                    | nginx не передає `Host`                      | `proxy_set_header Host $host;` (вже у шаблоні)                                                |
| `Connection refused` до сокета              | сервіс не стартував                          | `tail /var/log/supervisor/tenants_back.log` (або `journalctl -u tenants_back -n 50`)          |
