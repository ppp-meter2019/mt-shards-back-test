"""
Gunicorn configuration for the tenants_back project.

Loaded with:
    gunicorn tenants_back.wsgi:application -c /srv/tenants_back/deploy/gunicorn.conf.py

Tweak `workers` to match your CPU count (rule of thumb: 2 * cores + 1).
"""

import multiprocessing
import os

# UNIX socket — nginx reads from this. The path here MUST match the
# `upstream` block in nginx.example.conf.
bind = "unix:/run/tenants_back.sock"

# Sets the socket file mode to 0770 — owner+group only, no world access.
umask = 0o007

# Worker processes. Override via env if you need to.
workers = int(os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "sync"
timeout = 60
graceful_timeout = 30
keepalive = 5

# Drop privileges. systemd unit also sets User=/Group=, but if you launch
# gunicorn directly these flags take effect.
user = os.environ.get("GUNICORN_USER", "tenants")
group = os.environ.get("GUNICORN_GROUP", "www-data")

# Logging.
accesslog = "/var/log/tenants_back/access.log"
errorlog = "/var/log/tenants_back/error.log"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Process name shown in `ps`/`htop` — handy when you have several gunicorns.
proc_name = "tenants_back"