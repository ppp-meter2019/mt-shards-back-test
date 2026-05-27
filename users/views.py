from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView  # noqa: F401

from .serializers import (
    PublicTokenObtainPairSerializer,
    TenantTokenObtainPairSerializer,
)


class PublicTokenObtainPairView(TokenObtainPairView):
    serializer_class = PublicTokenObtainPairSerializer


class TenantTokenObtainPairView(TokenObtainPairView):
    serializer_class = TenantTokenObtainPairSerializer