from rest_framework import viewsets

from users.permissions import IsCompanyAdminOrReadOnly

from .models import Product
from .serializers import ProductSerializer


class ProductViewSet(viewsets.ModelViewSet):
    """Customers browse, admins curate the catalog."""

    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsCompanyAdminOrReadOnly]