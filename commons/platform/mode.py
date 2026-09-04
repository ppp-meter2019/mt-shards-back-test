"""Single source of truth for the run-mode flag, resolvable WITHOUT django.conf.

`use_multitenant()` is settings-load-safe: it does NOT touch `django.conf.settings`
(reading that during settings.py execution would cache an incomplete settings object).
It mirrors the bootstrap precedence used in settings.py: env → settings_mode.py → default.

Used by settings_base.py (to set USE_MULTITENANT), by the settings.py dispatcher (to pick
its branch) AND by helpers that may run at settings
load — notably commons.platform.beat.scoped_schedule, which the host project calls while building
CELERY_BEAT_SCHEDULE. Runtime-only helpers (task_queue, beat_conf) may read
settings.* directly; this is for the bootstrap flag only.
"""
import os


def use_multitenant():
    """Resolve USE_MULTITENANT: env USE_MULTITENANT=0/1 → settings_mode.py → default False."""
    if "USE_MULTITENANT" in os.environ:
        return os.environ["USE_MULTITENANT"] == "1"
    try:
        from tenants_back.settings_mode import USE_MULTITENANT  # gitignored, optional
        return bool(USE_MULTITENANT)
    except ImportError:
        return False


def bootstrap_float(name, default):
    """Resolve a LOAD-TIME float knob the same way as use_multitenant(): env → settings_mode.py
    → default. For values consumed while settings.py is still executing (e.g. the fanout beat
    tick baked into CELERY_BEAT_SCHEDULE), where django.conf.settings / settings_local.py are
    not available yet. A malformed value fails LOUDLY (float() raises) — never silently ignored;
    an absent one falls through to `default`."""
    if name in os.environ:
        return float(os.environ[name])                    # malformed env => ValueError (loud)
    try:
        import tenants_back.settings_mode as mode          # gitignored, optional
    except ImportError:
        return default
    return float(getattr(mode, name, default))             # missing attr => default; bad value => loud
