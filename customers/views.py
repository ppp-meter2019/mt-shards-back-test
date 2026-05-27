from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from users.models import User
from users.permissions import IsCompanyAdmin, IsCompanyAdminOrCustomer

from .models import Customer
from .serializers import CustomerSerializer


class CustomerViewSet(viewsets.ModelViewSet):
    """Company admins manage all customers; a logged-in customer can fetch
    their own profile via GET /api/customers/me/."""

    queryset = Customer.objects.select_related("user").all()
    serializer_class = CustomerSerializer

    def get_permissions(self):
        if self.action == "me":
            return [IsCompanyAdminOrCustomer()]
        return [IsCompanyAdmin()]

    @action(detail=False, methods=["get", "patch"])
    def me(self, request):
        if request.user.role != User.Role.CUSTOMER:
            raise NotFound("Only customers have a customer profile.")
        try:
            customer = request.user.customer_profile
        except Customer.DoesNotExist:
            raise NotFound("Customer profile is not set up for this account.")
        if request.method == "GET":
            return Response(CustomerSerializer(customer).data)
        serializer = CustomerSerializer(customer, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)