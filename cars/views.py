from rest_framework import viewsets

from users.permissions import IsCompanyAdmin

from .models import Car
from .serializers import CarSerializer


class CarViewSet(viewsets.ModelViewSet):
    """Only company-admins manage the fleet."""

    queryset = Car.objects.all()
    serializer_class = CarSerializer
    permission_classes = [IsCompanyAdmin]