from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError

from customers.models import Customer
from users.models import User
from users.permissions import IsCompanyAdminOrCustomer

from .models import Order
from .serializers import OrderSerializer


class OrderViewSet(viewsets.ModelViewSet):
    """
    - Company admin: full CRUD over every order, may set `customer` explicitly.
    - Customer: sees / mutates only their own orders; `customer` is forced to
      their own profile on create.
    - Driver: not allowed here; routes are their entry point.
    """

    serializer_class = OrderSerializer
    permission_classes = [IsCompanyAdminOrCustomer]

    def get_queryset(self):
        qs = Order.objects.select_related("customer__user").prefetch_related("items__product")
        user = self.request.user
        if user.role == User.Role.CUSTOMER:
            return qs.filter(customer__user=user)
        return qs

    def _customer_for_request(self) -> Customer:
        try:
            return self.request.user.customer_profile
        except Customer.DoesNotExist:
            raise ValidationError(
                "Customer profile is not set up for this account; ask the company admin."
            )

    def perform_create(self, serializer):
        user = self.request.user
        if user.role == User.Role.CUSTOMER:
            # Force ownership; ignore whatever 'customer' the caller passed.
            serializer.save(customer=self._customer_for_request())
        else:
            if "customer" not in serializer.validated_data:
                raise ValidationError({"customer": "This field is required."})
            serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance
        if user.role == User.Role.CUSTOMER:
            if instance.customer.user_id != user.id:
                raise PermissionDenied("You can only edit your own orders.")
            # don't let a customer reassign ownership
            serializer.save(customer=instance.customer)
        else:
            serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if user.role == User.Role.CUSTOMER and instance.customer.user_id != user.id:
            raise PermissionDenied("You can only delete your own orders.")
        instance.delete()