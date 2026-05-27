"""
The project uses two URL confs (see settings):
- PUBLIC_SCHEMA_URLCONF = tenants_back.urls_public
- ROOT_URLCONF          = tenants_back.urls_tenant

This module is kept only as a fallback alias for tooling that imports
`tenants_back.urls` directly.
"""

from .urls_tenant import urlpatterns  # noqa: F401