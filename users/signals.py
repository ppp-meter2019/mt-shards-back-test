"""Session auth signals.

Stamps the active tenant schema onto the session at login, so
SchemaBoundSessionMiddleware can reject a session replayed on another tenant.
This is the session-side analog of the `schema` claim that the JWT serializers
put on tokens (see users/serializers.py) and SchemaBoundJWTAuthentication checks.
"""

from django.contrib.auth.signals import user_logged_in
from django.db import connection
from django.dispatch import receiver


@receiver(user_logged_in)
def stamp_tenant_schema(sender, request, user, **kwargs):
    """Bind a freshly authenticated session to the tenant it was issued on.

    Runs inside auth.login() (before the session is saved), so `schema` is
    persisted in the same final save as the auth keys — an authenticated
    session therefore always carries `schema`.
    """
    if request is not None and hasattr(request, "session"):
        request.session["schema"] = connection.schema_name