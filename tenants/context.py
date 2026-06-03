"""ContextVar that carries the active database alias per request/task.

Set by DynamicDatabaseMiddleware (HTTP) or use_alias() (shell, scripts).
Read by TenantDatabaseRouter on every ORM call.
"""

from contextlib import contextmanager
from contextvars import ContextVar

current_db: ContextVar[str] = ContextVar("current_db", default="default")


@contextmanager
def use_alias(alias: str):
    """Set current_db for the duration of the with-block.

    Use outside the HTTP cycle (management commands, ad-hoc shell scripts).
    """
    token = current_db.set(alias)
    try:
        yield
    finally:
        current_db.reset(token)
