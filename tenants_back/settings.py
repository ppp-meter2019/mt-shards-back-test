"""Settings entry point: picks the run mode, then applies THAT MODE's layers in order.

DJANGO_SETTINGS_MODULE stays `tenants_back.settings` everywhere (manage.py, wsgi, asgi,
celery, bin/gunicorn_start.sh, scripts/) — the mode is chosen HERE rather than by pointing
Django at a different module, so a deployment switches modes with an env var and no config
edits.

Layer order — the tiers of deploy/standalone_multitenant_design.md §3.1:
  1. settings_base          everything shared; standalone needs nothing beyond it.
  2. settings_multitenant   the MT overlay, only when USE_MULTITENANT. It star-imports the
                            base ITSELF, so the base is fully built before the overlay's
                            first line — there is no partially-initialized module and no
                            ordering contract between them.
  3. the mode's local file  production overrides, LAST, so they win over base AND overlay:
                            settings_local.py (standalone) / settings_local_multitenant.py.

There is ONE LOCAL FILE PER MODE, and that is the point: a single file shared by both could
not work. A deployed multi-tenant one pins ENGINE to the django-tenants backend and declares
tenant_* shards — loading it under USE_MULTITENANT=0 yields a "standalone" config running on
the multi-tenant backend. scripts/ci_mode.sh used to work around exactly that by REQUIRING
the file to be absent in CI. Two names make the separation structural instead.

Standalone keeps the PLAIN name (settings_local.py) because standalone is the host project's
mode: the MT layer drops into a project that already has a settings_local.py, and nothing on
that side has to be renamed. Only the multi-tenant file carries a suffix.

The load stays HERE rather than at the bottom of settings_base / settings_multitenant:
  * a local file does `from .settings import DATABASES, ...`, and that resolves ONLY because
    by this point this module already has those names bound by the star-import above. Loaded
    from inside settings_multitenant.py instead, `tenants_back.settings` would still be a
    partially-initialized module holding nothing — ImportError.
  * settings_base would need an `if USE_MULTITENANT` at its bottom, or the overlay's
    `from .settings_base import *` would drag the STANDALONE production overrides into MT.
    Not having that `if` in the base is the whole point of the split.

use_multitenant() rather than settings.USE_MULTITENANT: django.conf.settings does not exist
while this module is executing. The base sets USE_MULTITENANT from the same function, so the
two can never disagree — do NOT define USE_MULTITENANT in a local file, which is applied
after INSTALLED_APPS / DATABASES / MIDDLEWARE have already been built for the other mode.

Both `except` clauses below are deliberately NARROW: only "this exact module does not exist"
is tolerated (a dev checkout with no local file). An ImportError raised INSIDE the local file
— a renamed helper, a missing package — now propagates. It used to be swallowed by a bare
`except ImportError` into a SILENT boot on dev defaults: DEBUG=True, ALLOWED_HOSTS=["*"], the
insecure SECRET_KEY, localhost DB. That happened for real when settings.py was split into
base + dispatcher.
"""
from commons.platform.mode import use_multitenant

if use_multitenant():
    from .settings_multitenant import *   # noqa: F401,F403  (itself built on settings_base)

    # Re-export the DB-OPTIONS helpers EXPLICITLY: `import *` skips underscore names, and a
    # deployed settings_local_multitenant.py imports them from HERE
    # (`from .settings import DATABASES, CACHES, _MT_DB_DEFAULTS, _aurora_db_options`).
    # _MT_DB_DEFAULTS carries the fields that belong to the MODE — a local file that spreads
    # it gets them, and keeps getting whatever is added to it later.
    # That file is gitignored and lives on the servers, so it cannot be migrated with the
    # code — this line is what keeps it working. Pinned by
    # test_settings_invariants.SettingsLocalContractTests. DO NOT REMOVE.
    from .settings_multitenant import (_MT_DB_DEFAULTS, _aurora_db_options,       # noqa: F401
                                       _proxy_db_options)

    try:
        from .settings_local_multitenant import *   # noqa: F401,F403
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.settings_local_multitenant":
            raise
        print("Can't load local settings (settings_local_multitenant.py)!")
else:
    from .settings_base import *          # noqa: F401,F403

    # No Aurora helpers here on purpose: they are multi-tenant deployment topology and live
    # in settings_multitenant.py. A standalone local file must not import them.

    try:
        from .settings_local import *    # noqa: F401,F403
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.settings_local":
            raise
        print("Can't load local settings (settings_local.py)!")
