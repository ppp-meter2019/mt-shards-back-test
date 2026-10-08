"""Single source of truth for the run-mode flag, resolvable WITHOUT django.conf.

`use_multitenant()` is settings-load-safe: it does NOT touch `django.conf.settings`
(reading that during settings.py execution would cache an incomplete settings object).
It mirrors the bootstrap precedence used in settings.py: env → settings_mode.py → default,
and REFUSES a value it cannot parse instead of falling back to the default.

Used by settings_base.py (to set USE_MULTITENANT), by the settings.py dispatcher (to pick
its branch) AND by helpers that may run at settings
load — notably commons.platform.beat.scoped_schedule, which the host project calls while building
CELERY_BEAT_SCHEDULE. Runtime-only helpers (task_queue, beat_conf) may read
settings.* directly; this is for the bootstrap flag only.
"""
import os

# django.core.exceptions does NOT import django.conf, so this stays settings-load-safe;
# SettingsLoadReentrancyTests fails if that ever stops being true.
from django.core.exceptions import ImproperlyConfigured

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _parse_bool(value: str, origin: str) -> bool:
    """Parse a boolean spelling, case- and whitespace-insensitive.

    An UNRECOGNISED value raises instead of falling back to the default. Widening the accepted
    spellings without this would only move the hole: `USE_MULTITENANT=ture` would still boot
    silently into the other mode, with the other mode's settings file.
    """
    token = value.strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    raise ImproperlyConfigured(
        f"{origin} is {value!r}, which is not a boolean. Use one of "
        f"{sorted(_TRUE)} / {sorted(_FALSE)} (case-insensitive)."
    )


def use_multitenant() -> bool:
    """Resolve USE_MULTITENANT: env → settings_mode.py → default False.

    An EMPTY env value counts as absent and falls through to the file. `docker run -e
    USE_MULTITENANT` and compose's `${USE_MULTITENANT}` both forward an empty string when the
    variable is unset on the host, and refusing to boot on that would punish the wrong mistake.
    """
    raw = os.environ.get("USE_MULTITENANT")
    if raw is not None and raw.strip():
        return _parse_bool(raw, "env USE_MULTITENANT")
    try:
        from tenants_back.settings_mode import USE_MULTITENANT  # gitignored, optional
    except ImportError:
        return False
    # A str in settings_mode.py goes through the same parser: bool("false") is True, which is
    # the identical silent inversion one layer down.
    if isinstance(USE_MULTITENANT, str):
        return _parse_bool(USE_MULTITENANT, "settings_mode.USE_MULTITENANT")
    return bool(USE_MULTITENANT)


def bootstrap_float(name: str, default: float) -> float:
    """Resolve a LOAD-TIME float knob the same way as use_multitenant(): env → settings_mode.py
    → default. For values consumed while settings.py is still executing (e.g. the fanout beat
    tick baked into CELERY_BEAT_SCHEDULE), where django.conf.settings / the local settings file are
    not available yet. A malformed value fails LOUDLY (float() raises) — never silently ignored;
    an absent one falls through to `default`."""
    if name in os.environ:
        return float(os.environ[name])                    # malformed env => ValueError (loud)
    try:
        import tenants_back.settings_mode as mode          # gitignored, optional
    except ImportError:
        return default
    return float(getattr(mode, name, default))             # missing attr => default; bad value => loud
