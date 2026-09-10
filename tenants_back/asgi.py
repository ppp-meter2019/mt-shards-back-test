"""ASGI entrypoint — present, but NOT the deployed one.

Every deploy path points at `tenants_back.wsgi` under gunicorn's `sync` (prefork) worker:
deploy/gunicorn.conf.py, deploy/gunicorn.service, bin/gunicorn_start.sh. This module exists
so the async path can be EXPLORED without recreating it, and the blocker is not this file:
a django-tenants connection carries the tenant's `search_path`, so it cannot be shared
across tenants, while Django's ASGI handler gives each request its own thread — and thus its
own connection per shard. `Why not ASGI` in bin/gunicorn_start.sh has the full chain.

NB settings.ASGI_APPLICATION is not a switch — Django does not read it (see the note there).
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tenants_back.settings")

application = get_asgi_application()
