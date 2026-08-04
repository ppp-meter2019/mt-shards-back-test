"""Tenant-bound session guard.

SchemaBoundSessionMiddleware rejects a session-authenticated request whose
session was issued on a different tenant than the one now being served — the
session-side analog of SchemaBoundJWTAuthentication. It closes cross-tenant
session replay independently of password/auth-hash semantics (so it holds even
if a backend ever creates users with an empty/shared password).

Placed AFTER AuthenticationMiddleware. It reads the session directly (not
request.user), so the decision never depends on the auth-hash check that an
empty-password bug can defeat.
"""

import logging

from django.contrib.auth import SESSION_KEY, logout
from django.db import connection
from django.shortcuts import redirect

logger = logging.getLogger("security")


class SchemaBoundSessionMiddleware:
    sync_capable = True
    async_capable = False

    # API paths are stateless (JWT-guarded); sessions are irrelevant there.
    # Mirrors the exemption in the session middleware added on the project merge.
    API_PREFIXES = ("/api/v1/", "/open_api/api/v1/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.API_PREFIXES):
            return self.get_response(request)

        session = getattr(request, "session", None)
        # Only authenticated sessions carry SESSION_KEY. Anonymous sessions and
        # JWT/API requests (no session user) are untouched.
        if session is not None and session.get(SESSION_KEY):
            if session.get("schema") != connection.schema_name:
                logger.warning(
                    "Cross-tenant session rejected: session schema=%r, "
                    "request schema=%r, uid=%r, path=%s",
                    session.get("schema"), connection.schema_name,
                    session.get(SESSION_KEY), request.path,
                )
                logout(request)  # flushes session, resets request.user
                return redirect("admin:login")  # graceful re-login (admin-only)
        return self.get_response(request)