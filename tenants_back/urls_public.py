"""URLs that are only resolvable on the public schema (the management host).

This is what tenant-administrators see: a tenants CRUD plus their own JWT
login. Business endpoints (cars, orders, ...) live in `urls_tenant.py` and
are only reachable via tenant subdomains.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from tenants.admin import public_admin_site
from tenants.views import TenantViewSet
from users.views import PublicTokenObtainPairView, TokenRefreshView

router = DefaultRouter()
router.register(r"tenants", TenantViewSet, basename="tenant")

urlpatterns = [
    path("admin/", public_admin_site.urls),
    path("api/auth/login/", PublicTokenObtainPairView.as_view(), name="public_login"),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="public_refresh"),
    path("api/", include(router.urls)),
]