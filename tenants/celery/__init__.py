# Importing app.py also connects its `celeryd_init` receiver (_guard_worker_pool), which
# refuses a cooperative worker pool at startup. NB the per-task schema switch is NOT a
# signal: it is the with-block in TenantTask.__call__ — see the docstrings in app.py/task.py
# for why a prerun/postrun pair was rejected.
from .app import CeleryApp

__all__ = ["CeleryApp"]
