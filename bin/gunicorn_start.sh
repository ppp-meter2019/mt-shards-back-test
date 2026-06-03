#!/bin/bash

NAME="tenants_back"                                                # Name of the application
DJANGODIR=/home/ubuntu/tenants_back                            # Django project directory
SOCKFILE=/home/ubuntu/tenants_back/run/gunicorn.sock           # we will communicate using this unix socket
USER=ubuntu                                                    # the user to run as
GROUP=www-data                                                    # the group to run as (nginx user's group → can read the socket)
NUM_WORKERS=5                                                     # how many worker processes should Gunicorn spawn (concurrency = workers)
WORKER_CLASS=sync                                                 # sync (prefork): one request per process. Project default.
DJANGO_SETTINGS_MODULE=tenants_back.settings                      # which settings file should Django use
DJANGO_WSGI_MODULE=tenants_back.wsgi                              # WSGI module name

# NOTE: this project runs SYNC prefork workers (WSGI). Concurrency = NUM_WORKERS
# processes; rule of thumb 2*cores + 1. CPU-heavy work goes to Celery, not here.
# To explore the async path (UvicornWorker/ASGI) you must change WORKER_CLASS +
# DJANGO_WSGI_MODULE→asgi here, flip WSGI/ASGI_APPLICATION in settings.py, AND
# the tenant/shard middleware would need rework — see the "Worker model" block
# in tenants_back/settings.py and README "Architecture trade-offs".

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
    --max-requests=10000 \
    --max-requests-jitter=1000 \
    --access-logfile=- \
    --error-logfile=-
