#!/bin/bash

NAME="tenants_back"                                                # Name of the application
DJANGODIR=/home/ubuntu/mt-shards-back-test                            # Django project directory
SOCKFILE=/home/ubuntu/mt-shards-back-test/run/gunicorn.sock           # we will communicate using this unix socket
USER=ubuntu                                                    # the user to run as
GROUP=ubuntu                                                    #
NUM_WORKERS=2                                                     # how many worker processes should Gunicorn spawn (concurrency = workers)
WORKER_CLASS=sync                                                 # sync (prefork). NOT a choice — see below.
DJANGO_SETTINGS_MODULE=tenants_back.settings                      # which settings file should Django use
DJANGO_WSGI_MODULE=tenants_back.wsgi                              # WSGI module name

# SYNC prefork over WSGI is the ONLY supported worker model here, and that follows from
# django-tenants rather than from preference. Concurrency = NUM_WORKERS processes; rule of
# thumb 2*cores + 1. CPU-heavy work goes to Celery, not here.
#
# Why not ASGI/UvicornWorker:
#   * A DB connection is TENANT-STATEFUL under django-tenants — the active schema lives on
#     the connection object AND as `SET search_path` on the physical session. Such a
#     connection cannot be shared or multiplexed across tenants, so correctness needs one
#     request per connection per unit of concurrency. Prefork gives that by construction.
#   * Django's ASGI handler runs each request inside its own ThreadSensitiveContext, and
#     `connections` is strictly thread-local (ConnectionHandler.thread_critical). So every
#     in-flight request would get its OWN connection PER SHARD: connection count stops
#     being bounded by process count, which is exactly what this deployment is sized around
#     (see "Connection sizing" in deploy/DATABASE_SETUP.md and the RDS Proxy notes).
#   * That per-request thread is torn down when the request ends, taking its connections
#     with it — so CONN_MAX_AGE is defeated and every request pays a fresh TCP+TLS
#     handshake to Aurora.
#   * And nothing is gained: the ORM stays synchronous either way, adapted via
#     sync_to_async(thread_sensitive=True). Async would trade processes for threads and add
#     a connection per concurrent request without making a single query non-blocking.
#   * On top of that, this project's own routing (the `current_db` ContextVar plus the
#     per-request schema switch on the shard connection) assumes non-cooperative
#     concurrency — the same reason tenants/celery/app.py REFUSES gevent/eventlet pools.
#
# The trap: django-tenants does not refuse an async stack. TenantMainMiddleware is a
# MiddlewareMixin, and MiddlewareMixin declares async_capable = True, so Django adapts it
# silently — an ASGI deployment boots and appears to work.
#
# NB tenants_back/asgi.py exists as an exploration entrypoint, but settings.ASGI_APPLICATION
# is NOT a switch: Django never reads it (it is a Channels setting) and
# get_asgi_application() does not consult it. Changing it has no effect.
# Background: "Worker model" in tenants_back/settings_base.py, README "Architecture
# trade-offs", docs/why-no-async.*.

set -e

echo "Starting $NAME as `whoami`"

# Activate the virtual environment
cd $DJANGODIR

source venv/bin/activate
echo "venv is activated"

export DJANGO_SETTINGS_MODULE=$DJANGO_SETTINGS_MODULE
export PYTHONPATH=$DJANGODIR:$PYTHONPATH
echo "export done"

# All production-specific values (SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS,
# CORS rules, DB credentials, …) live in tenants_back/settings_local.py — copy it
# from tenants_back/settings_local.py.example before first start.

# Create the run directory if it doesn't exist (tmpfs and reboots may wipe it)
RUNDIR=$(dirname $SOCKFILE)
test -d $RUNDIR || mkdir -p $RUNDIR
echo "rundir ready"

# Start Gunicorn
exec venv/bin/gunicorn ${DJANGO_WSGI_MODULE}:application \
    --name $NAME \
    --workers $NUM_WORKERS \
    --worker-class $WORKER_CLASS \
    --user=$USER --group=$GROUP \
    --bind=unix:$SOCKFILE \
    --timeout=120 \
    --max-requests=100 \
    --max-requests-jitter=100 \
    --access-logfile=- \
    --log-file=- \
    --error-logfile=-
