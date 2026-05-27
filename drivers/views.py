from rest_framework import viewsets

from users.permissions import IsCompanyAdmin

from .models import Driver
from .serializers import DriverSerializer


class DriverViewSet(viewsets.ModelViewSet):
    queryset = Driver.objects.select_related("user").all()
    serializer_class = DriverSerializer
    permission_classes = [IsCompanyAdmin]