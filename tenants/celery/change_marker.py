"""Per-tenant beat change-markers in Redis.

Each schema has one marker key `beat:last_change:<schema>` holding the epoch time
its schedule last changed (like an mtime). Beat reads them all in ONE MGET per
tick (see TenantAwarePeriodicTasks.last_change) and reloads when the max advances
— no per-tenant DB round-trip.

Markers live in a DEDICATED cache alias ("beat") with NO per-tenant key prefix:
they are cross-tenant coordination keys, written in a tenant's context (signal)
but read by beat outside any tenant context, so a per-tenant prefix would split
writer and reader onto different keys.

TTL = BEAT_MARKER_TTL_SECONDS (= k×max_interval, k>=3): safely larger than
max_interval so a running beat always observes a marker before it expires, and
deleted-tenant markers self-clean. Timestamps are monotonic, so a real change is
always detected even if a key expired meanwhile. Direct-SQL / bulk changes bypass
the ORM signal — run `manage.py resync_beat_schedules`.
"""
import time

from django.conf import settings
from django.core.cache import caches
from django.db.models.signals import post_delete, post_save
from django_celery_beat.models import (
    ClockedSchedule, CrontabSchedule, IntervalSchedule, PeriodicTask, SolarSchedule,
)

from .compat import current_schema_name, get_public_schema_name

_KEY_PREFIX = "beat:last_change:"
_SCHEDULE_MODELS = (
    PeriodicTask, CrontabSchedule, IntervalSchedule, SolarSchedule, ClockedSchedule,
)
_SCHEMA_CACHE_TTL = 30                       # seconds to cache the tenant list for reads
_schema_cache = {"ts": 0.0, "names": None}


def _cache():
    return caches["beat"]


def _ttl():
    return getattr(settings, "BEAT_MARKER_TTL_SECONDS", 15)


def marker_key(schema):
    return f"{_KEY_PREFIX}{schema}"


def bump(instance=None, **kwargs):
    """Signal handler: mark the CURRENT schema's schedule changed. The schema is
    read from the active connection (in-memory, no DB; current_schema_name already
    falls back to public).

    CRITICAL: skip beat's own internal last_run_at/total_run_count writes
    (no_changes=True). django-celery-beat sets that flag when persisting run
    bookkeeping (schedulers.py sets model.no_changes=True around save); without
    this guard every task run would bump the marker → beat reloads on nearly every
    tick (feedback loop). Real user edits leave no_changes=False → bump fires.
    """
    if getattr(instance, "no_changes", False):
        return
    _cache().set(marker_key(current_schema_name()), time.time(), timeout=_ttl())


def _schema_names():
    now = time.monotonic()
    if _schema_cache["names"] is None or now - _schema_cache["ts"] > _SCHEMA_CACHE_TTL:
        from tenants.models import Tenant                       # lazy: avoid app-registry cycles
        _schema_cache["names"] = [
            get_public_schema_name(),
            *Tenant.objects.values_list("schema_name", flat=True),
        ]
        _schema_cache["ts"] = now
    return _schema_cache["names"]


def marker_max():
    """Newest change-time across all schemas (epoch float) via ONE MGET; 0.0 if none."""
    vals = _cache().get_many([marker_key(s) for s in _schema_names()])
    return max(vals.values(), default=0.0)


def bump_all():
    """Set every schema's marker to now — forces a running beat to fully reload
    all schedules on its next tick. Used by `resync_beat_schedules`."""
    schemas = _schema_names()
    _cache().set_many({marker_key(s): time.time() for s in schemas}, timeout=_ttl())
    return len(schemas)


def connect_beat_change_signals():
    """Wire ORM save/delete on schedule models to bump the current schema's marker.
    Idempotent (dispatch_uid), so calling twice is harmless."""
    for model in _SCHEDULE_MODELS:
        post_save.connect(bump, sender=model, weak=False,
                          dispatch_uid=f"beat_bump_{model.__name__}_save")
        post_delete.connect(bump, sender=model, weak=False,
                            dispatch_uid=f"beat_bump_{model.__name__}_del")
