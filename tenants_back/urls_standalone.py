"""Minimal ROOT_URLCONF for STANDALONE mode (USE_MULTITENANT=False).

This is a STUB for this repo only: its sole job is to let the app boot and CI
run against a single default DB. At integration the larger host project supplies
its own urls.py as ROOT_URLCONF and this module is NOT merged (like the `routes`
offline commands).

It deliberately imports NOTHING from `tenants` (that app is not installed in
standalone) and uses stock SimpleJWT views (no per-tenant `schema` claim).
"""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from cars.views import CarViewSet
from customers.views import CustomerViewSet
from drivers.views import DriverViewSet
from orders.views import OrderViewSet
from products.views import ProductViewSet
from routes.views import RouteViewSet


def health(_request):
    """Liveness probe — no DB, no tenant context."""
    return JsonResponse({"status": "ok", "mode": "standalone"})


router = DefaultRouter()
router.register(r"cars", CarViewSet, basename="car")
router.register(r"drivers", DriverViewSet, basename="driver")
router.register(r"customers", CustomerViewSet, basename="customer")
router.register(r"products", ProductViewSet, basename="product")
router.register(r"orders", OrderViewSet, basename="order")
router.register(r"routes", RouteViewSet, basename="route")

urlpatterns = [
    path("api/health/", health),
    path("admin/", admin.site.urls),
    path("api/auth/login/", TokenObtainPairView.as_view(), name="login"),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="refresh"),
    path("api/", include(router.urls)),
]
