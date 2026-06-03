"""Diagnostic-headers middleware: stamps each response with runtime identity.

Used by the MVP frontend to render a "served by host:pid -> alias" strip at
the top of every page so operators can visually confirm:
  - ALB is round-robining requests across the backend EC2 instances,
  - tenant subdomains are routing to the correct Aurora shard,
  - which gunicorn worker / thread handled the call.

Placement: INSIDE (after) TenantShardRoutingMiddleware in settings.MIDDLEWARE so
that the `current_db` ContextVar (set there) is still live when we read it -
that outer middleware resets the ContextVar in a finally block after
get_response() returns.

Supports both sync and async stacks. Django picks the right __call__ based on
whether get_response is a coroutine function; the project runs sync (prefork)
workers, but the class stays unchanged if the worker model ever changes.

CORS note: in split-origin dev the browser hides custom headers unless they
are listed in Access-Control-Expose-Headers - we merge them in here. Under
ALB (single origin) the CORS layer is bypassed and this header is harmless.
"""

import os
import socket
import threading

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

from .context import current_db


# Resolved once at import time - hostname doesn't change for the life of a
# worker process. On AWS EC2 this is the instance's private DNS name unless
# you've set a custom hostname.
_HOSTNAME = socket.gethostname()

_DIAGNOSTIC_HEADERS = (
    "X-Served-By",
    "X-Worker-Pid",
    "X-Thread-Id",
    "X-DB-Alias",
)


class DiagnosticsHeadersMiddleware:
    """Adds runtime identity headers to every response."""

    sync_capable  = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self._is_async:
            return self.__acall__(request)
        return self._stamp(self.get_response(request))

    async def __acall__(self, request):
        response = await self.get_response(request)
        return self._stamp(response)

    @staticmethod
    def _stamp(response):
        response["X-Served-By"]  = _HOSTNAME
        response["X-Worker-Pid"] = str(os.getpid())
        response["X-Thread-Id"]  = str(threading.get_ident())
        try:
            response["X-DB-Alias"] = current_db.get()
        except LookupError:
            response["X-DB-Alias"] = "default"

        # Merge into any Access-Control-Expose-Headers already set by CORS
        # so the browser can read these in split-origin dev.
        existing = response.get("Access-Control-Expose-Headers", "")
        existing_names = {h.strip() for h in existing.split(",") if h.strip()}
        merged = sorted(existing_names.union(_DIAGNOSTIC_HEADERS))
        response["Access-Control-Expose-Headers"] = ", ".join(merged)

        return response
