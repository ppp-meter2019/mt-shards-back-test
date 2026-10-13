"""Migrator settings invariants (deploy/migrations_plan_rev7.md §13). MT-only.

  E020  a TENANT_MULTIPROCESSING_* setting is set: upstream's multiprocessing executor is
        not used — the migrator has its own pool (--jobs / --jobs-per-shard)
  W021  env EXECUTOR is set: upstream would pick an executor from it; migrate_schemas
        refuses anything but `standard`
  E022  TENANT_MIGRATE_JOBS / _JOBS_PER_SHARD not an int >= 1 (bool rejected), or
        PER_SHARD > JOBS
  E024  TENANT_MIGRATE_DDL_LOCK_TIMEOUT not ``0`` / ``<n>ms`` / ``<n>s`` / ``<n>min``
  E029  TENANT_MIGRATE_LOG_DIR missing or not a path
"""
import os
import re
from typing import Any

from django.apps import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error, Warning, register

from .base import mt_check

_LOCK_TIMEOUT_RE = re.compile(r"^(0|\d+(ms|s|min))$")


@register()
@mt_check
def migrator_settings(app_configs: list[AppConfig] | None, **kwargs: Any) -> list[CheckMessage]:
    out: list[CheckMessage] = []

    upstream = sorted(n for n in dir(settings) if n.startswith("TENANT_MULTIPROCESSING_"))
    if upstream:
        out.append(Error(
            f"{', '.join(upstream)} set: django-tenants' multiprocessing executor is not used.",
            hint="Remove it; parallelism is migrate_schemas --jobs / --jobs-per-shard "
                 "(TENANT_MIGRATE_JOBS / TENANT_MIGRATE_JOBS_PER_SHARD).",
            id="tenants.E020"))

    if os.environ.get("EXECUTOR"):
        out.append(Warning(
            f"Environment variable EXECUTOR={os.environ['EXECUTOR']!r} is set.",
            hint="django-tenants would choose its migration executor from it; migrate_schemas "
                 "accepts only `standard` and runs its own scheduler. Unset it.",
            id="tenants.W021"))

    jobs = getattr(settings, "TENANT_MIGRATE_JOBS", None)
    per_shard = getattr(settings, "TENANT_MIGRATE_JOBS_PER_SHARD", None)
    bad = [name for name, v in (("TENANT_MIGRATE_JOBS", jobs),
                                ("TENANT_MIGRATE_JOBS_PER_SHARD", per_shard))
           if isinstance(v, bool) or not isinstance(v, int) or v < 1]
    if bad:
        out.append(Error(f"{', '.join(bad)} must be an integer >= 1 "
                         f"(got JOBS={jobs!r}, JOBS_PER_SHARD={per_shard!r}).",
                         id="tenants.E022"))
    elif per_shard > jobs:
        out.append(Error(f"TENANT_MIGRATE_JOBS_PER_SHARD ({per_shard}) exceeds "
                         f"TENANT_MIGRATE_JOBS ({jobs}).", id="tenants.E022"))

    lt = getattr(settings, "TENANT_MIGRATE_DDL_LOCK_TIMEOUT", None)
    if not isinstance(lt, str) or not _LOCK_TIMEOUT_RE.match(lt):
        out.append(Error(
            f"TENANT_MIGRATE_DDL_LOCK_TIMEOUT={lt!r} is not a PostgreSQL duration the "
            f"migrator accepts.",
            hint="Use '0' (no timeout), '<n>ms', '<n>s' or '<n>min', e.g. '5s'.",
            id="tenants.E024"))

    log_dir = getattr(settings, "TENANT_MIGRATE_LOG_DIR", None)
    if not isinstance(log_dir, (str, os.PathLike)) or not os.fspath(log_dir):
        out.append(Error(
            f"TENANT_MIGRATE_LOG_DIR={log_dir!r} is not a path.",
            hint="migrate_schemas writes per-run folders of per-tenant logs there (§9.9); "
                 "in an ephemeral container point it at a mounted volume.",
            id="tenants.E029"))
    return out
