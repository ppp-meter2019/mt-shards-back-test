from kombu.exceptions import OperationalError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from users.permissions import IsCompanyAdminOrReadOnly

from .models import Product
from .serializers import ProductSerializer


class ProductViewSet(viewsets.ModelViewSet):
    """Customers browse, admins curate the catalog."""

    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsCompanyAdminOrReadOnly]

    @action(detail=False, methods=["post"])
    def generate(self, request):
        """Queue generate_products for THIS tenant (runs async on a worker).

        Reachable only on a tenant host, so the request is already in the
        tenant's schema/shard context → the enqueued task carries that schema
        (tenants.celery.headers_with_schema) and the worker writes into this
        tenant's products table. POST requires company_admin (the viewset's
        IsCompanyAdminOrReadOnly).
        """
        from .tasks import generate_products

        try:
            result = generate_products.delay()
        except OperationalError:
            return Response(
                {"detail": "Task queue is unavailable. Try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {"detail": "Generation queued.", "task_id": result.id, "count": 2},
            status=status.HTTP_202_ACCEPTED,
        )
