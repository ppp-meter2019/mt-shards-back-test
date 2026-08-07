"""Tenant error responses with content negotiation: JSON for API/JSON clients,
branded self-contained HTML for browsers. Templates render with NO tenant context,
so they work even when the tenant's schema is missing/broken. No PHI/PII (HIPAA).

The error path must never itself raise: if a template is missing/broken, fall back
to a minimal inline page.
"""
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils.html import escape


def wants_json(request):
    return (request.path.startswith(settings.API_PATH_PREFIXES)
            or "application/json" in request.META.get("HTTP_ACCEPT", ""))


def error_response(request, *, status, code, detail, template, retry_after=None, extra=None):
    if wants_json(request):
        body = {"detail": detail, "code": code}
        if extra:
            body.update(extra)
        resp = JsonResponse(body, status=status)
    else:
        try:
            html = render_to_string(template, {"detail": detail, "code": code})
        except Exception:   # missing/broken template — the error path must not itself 500
            html = f"<!doctype html><title>{status}</title><h1>{status}</h1><p>{escape(detail)}</p>"
        resp = HttpResponse(html, status=status, content_type="text/html; charset=utf-8")
    resp["Cache-Control"] = "no-store"                 # don't let a proxy/CDN cache a transient error
    if retry_after is not None:
        resp["Retry-After"] = str(retry_after)
    return resp