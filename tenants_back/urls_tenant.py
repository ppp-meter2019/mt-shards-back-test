"""URLs visible inside a tenant schema (alpha.localhost, beta.localhost, ...).

Business endpoints + JWT login + per-tenant Django admin live here.
"""

from django.contrib import admin
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from cars.views import CarViewSet
from customers.views import CustomerViewSet
from drivers.views import DriverViewSet
from orders.views import OrderViewSet
from products.views import ProductViewSet
from routes.views import RouteViewSet
from users.views import TenantTokenObtainPairView, TokenRefreshView

router = DefaultRouter()
router.register(r"cars", CarViewSet, basename="car")
router.register(r"drivers", DriverViewSet, basename="driver")
router.register(r"customers", CustomerViewSet, basename="customer")
router.register(r"products", ProductViewSet, basename="product")
router.register(r"orders", OrderViewSet, basename="order")
router.register(r"routes", RouteViewSet, basename="route")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/login/", TenantTokenObtainPairView.as_view(), name="tenant_login"),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="tenant_refresh"),
    path("api/", include(router.urls)),
]