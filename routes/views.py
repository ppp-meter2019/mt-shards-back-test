from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from users.models import User
from users.permissions import IsCompanyAdmin

from .models import Route
from .serializers import RouteSerializer


class RouteViewSet(viewsets.ModelViewSet):
    """Admins manage routes; drivers see (read-only) the routes they're on."""

    serializer_class = RouteSerializer

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsAuthenticated()]
        return [IsCompanyAdmin()]

    def get_queryset(self):
        qs = Route.objects.select_related("driver__user", "car").prefetch_related("orders")
        user = self.request.user
        if user.role == User.Role.DRIVER:
            return qs.filter(driver__user=user)
        if user.role == User.Role.CUSTOMER:
            return qs.filter(orders__customer__user=user).distinct()
        return qs