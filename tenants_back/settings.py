"""Settings entry point: picks the run mode, then applies the layers in order.

DJANGO_SETTINGS_MODULE stays `tenants_back.settings` everywhere (manage.py, wsgi, asgi,
celery, bin/gunicorn_start.sh, scripts/) — the mode is chosen HERE rather than by pointing
Django at a different module, so a deployment switches modes with an env var and no config
edits.

Layer order — the three tiers of deploy/standalone_multitenant_design.md §3.1:
  1. settings_base         everything shared; standalone needs nothing beyond it.
  2. settings_multitenant  the MT overlay, only when USE_MULTITENANT. It star-imports the
                           base ITSELF, so the base is fully built before the overlay's first
                           line — there is no partially-initialized module and no ordering
                           contract between them.
  3. settings_local        production overrides, LAST, so they win over base AND overlay.

use_multitenant() rather than settings.USE_MULTITENANT: django.conf.settings does not exist
while this module is executing. The base sets USE_MULTITENANT from the same function, so the
two can never disagree — do NOT define USE_MULTITENANT in settings_local.py, which is applied
after INSTALLED_APPS / DATABASES / MIDDLEWARE have already been built for the other mode.
"""
from commons.platform.mode import use_multitenant

if use_multitenant():
    from .settings_multitenant import *   # noqa: F401,F403  (itself built on settings_base)
else:
    from .settings_base import *          # noqa: F401,F403

# Re-export the DB-OPTIONS helpers EXPLICITLY: `import *` skips underscore names, and a
# deployed settings_local.py imports them from HERE
# (`from .settings import DATABASES, CACHES, _aurora_db_options, _proxy_db_options`).
# settings_local.py is gitignored and lives on the servers, so it cannot be migrated with the
# code — this line is what keeps it working. Without it the import below raises ImportError,
# which the except swallows into a SILENT fallback to dev defaults: DEBUG=True,
# ALLOWED_HOSTS=["*"], the insecure SECRET_KEY, localhost DB. Pinned by
# test_settings_invariants.SettingsLocalContractTests. DO NOT REMOVE.
from .settings_base import _aurora_db_options, _proxy_db_options  # noqa: F401

try:
    from .settings_local import *         # noqa: F401,F403
except ImportError:
    print("Can\'t load local settings!")
