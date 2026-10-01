"""Project Celery app — uses our shard+schema-aware CeleryApp (tenants.celery).

Config comes from Django settings under the CELERY_ namespace; tasks are
autodiscovered from each app's tasks.py (e.g. tenants/tasks.py).
"""
import os

# DJANGO_SETTINGS_MODULE MUST be set before touching settings / importing tenants.celery:
# reading settings resolves USE_MULTITENANT, and (multitenant) importing tenants.celery
# pulls in django_tenants.utils, whose schema_exists()/schema_rename() default args
# evaluate get_tenant_database_alias() (reads settings.TENANT_DB_ALIAS) at IMPORT time —
# without the env var that raises ImproperlyConfigured on a clean worker host.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tenants_back.settings")

from commons.platform.mode import use_multitenant  # noqa: E402

# NOTHING in this module may read django.conf.settings at import time. Importing the settings
# PACKAGE runs tenants_back/__init__.py first (the standard Celery-Django integration:
# `from .celery import app as celery_app`), which lands here — so a settings read on this line
# makes Django resolve the settings module RE-ENTRANTLY, from inside the partially initialised
# package that contains it. It works today only because that import is the first statement in
# __init__.py and settings_mode.py is a leaf, neither of which is enforced; point
# DJANGO_SETTINGS_MODULE at a module that star-imports this project's settings and it breaks
# (verified: django.setup() succeeds with DATABASES == {} and no USE_MULTITENANT).
#
# The standard celery.py has no such read — `Celery(name)` and
# `config_from_object("django.conf:settings")` are both lazy — so this is our deviation, not
# the integration pattern's.

# Mode gate: multitenant uses the shard+schema-aware CeleryApp (per-task tenant_context
# via TenantTask.__call__); standalone uses a plain Celery app (single DB, no schema
# headers, no context switch). This also means standalone NEVER imports tenants.celery /
# django_tenants — so django-tenants is not required to boot Celery there.
#
# use_multitenant() rather than settings.USE_MULTITENANT: same flag, same source of truth
# (settings_base.py sets USE_MULTITENANT from this very function), but resolved from
# env → settings_mode.py → default without touching django.conf — see commons/platform/mode.py.
if use_multitenant():
    from tenants.celery import CeleryApp  # noqa: E402
    app = CeleryApp("tenants_back")
else:
    from celery import Celery  # noqa: E402
    app = Celery("tenants_back")

app.config_from_object("django.conf:settings", namespace="CELERY")

if use_multitenant():
    # RedBeat's is_key_in_conf() tests membership with `in`, which does NOT report keys
    # loaded via the CELERY_ namespace (a Celery ConfigurationView quirk) — so RedBeat
    # emits a "set redbeat_redis_url explicitly" deprecation even though the value IS
    # present and used. Set it directly so the membership check passes.
    #
    # Not cosmetic for long. CHANGES.txt for 2.4.2 (our pinned version) says "RedBeat 2.5.0
    # will require redbeat_redis_url", and requirements.txt allows >=2.2,<3 — a routine
    # upgrade brings a version that needs the key PRESENT, which the CELERY_ namespace alone
    # never provides. Upstream documents only the celeryconfig.py form (REDBEAT_REDIS_URL),
    # where the key lands in conf.keys() directly; Django + config_from_object(namespace=...)
    # is outside what they document, which is why their check misses it. See
    # github.com/sibson/redbeat#169 for the same setup yielding conf.redis_url = None.
    #
    # The VALUE here is deliberately read from the environment, not from settings: reading
    # settings would reintroduce the re-entrant load described at the top of this module, and
    # it buys nothing, because a namespace-loaded CELERY_REDBEAT_REDIS_URL SHADOWS whatever is
    # assigned here (verified: assigning a sentinel leaves the effective value untouched).
    # Three-way outcome, in order of precedence:
    #   settings define CELERY_REDBEAT_REDIS_URL  -> that value wins; this line only registers
    #                                                the key. This is the normal case:
    #                                                settings_multitenant.py always defines it.
    #   they do not, but CELERY_BROKER_URL is set -> the broker URL, i.e. RedBeat's own
    #                                                historical fallback, kept alive by hand.
    #   neither                                   -> "" -> `celery beat` refuses to start with
    #                                                ValueError from Redis.from_url. Deliberate:
    #                                                a scheduler pointing nowhere must not come
    #                                                up quietly. Workers and web are unaffected
    #                                                (nothing else builds RedBeatConfig), so
    #                                                this surfaces as beat being down — watch
    #                                                for it externally.
    app.conf.redbeat_redis_url = os.environ.get("CELERY_BROKER_URL", "")

app.autodiscover_tasks()
