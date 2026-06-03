#!/bin/bash

NAME="tenants_back"                                                # Name of the application
DJANGODIR=/home/ubuntu/tenants_back                            # Django project directory
SOCKFILE=/home/ubuntu/tenants_back/run/gunicorn.sock           # we will communicate using this unix socket
USER=ubuntu                                                    # the user to run as
GROUP=www-data                                                    # the group to run as (nginx user's group → can read the socket)
NUM_WORKERS=5                                                     # how many worker processes should Gunicorn spawn
WORKER_CLASS=uvicorn.workers.UvicornWorker                        # ASGI worker (async). For sync, see note below.
DJANGO_SETTINGS_MODULE=tenants_back.settings                      # which settings file should Django use
DJANGO_ASGI_MODULE=tenants_back.asgi                              # ASGI module name

# NOTE: this project runs ASGI by default (UvicornWorker). To switch to a sync
# server set WORKER_CLASS="gthread" (add --threads), point at tenants_back.wsgi
# instead of .asgi, and flip ASGI/WSGI in settings.py — see the "Worker model"
# block in tenants_back/settings.py.

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
exec venv/bin/gunicorn ${DJANGO_ASGI_MODULE}:application \
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
