"""JWT login serializers.

Two variants because the rule "where can this user log in?" is per-schema:

- PublicTokenObtainPairSerializer: only role=tenant_admin may obtain a token,
  and only when the request hits the public schema.
- TenantTokenObtainPairSerializer: tenant_admin may NOT obtain a token here
  (their identity belongs in the public DB); the other three roles can.

Both embed `role` and `schema` claims into the access token so downstream
services / clients don't have to look them up.
"""

from django.db import connection
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import User


class _BaseTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["schema"] = connection.schema_name
        token["username"] = user.username
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["role"] = self.user.role
        data["schema"] = connection.schema_name
        return data


class PublicTokenObtainPairSerializer(_BaseTokenObtainPairSerializer):
    """Public-host login: only tenant administrators allowed."""

    def validate(self, attrs):
        data = super().validate(attrs)
        if connection.schema_name != "public":
            raise serializers.ValidationError(
                "Tenant administrators must log in on the management host."
            )
        if self.user.role != User.Role.TENANT_ADMIN:
            raise serializers.ValidationError(
                "This endpoint accepts tenant administrators only."
            )
        return data


class TenantTokenObtainPairSerializer(_BaseTokenObtainPairSerializer):
    """Tenant-host login: company admin / customer / driver."""

    def validate(self, attrs):
        data = super().validate(attrs)
        if connection.schema_name == "public":
            raise serializers.ValidationError(
                "This endpoint is only available on tenant subdomains."
            )
        if self.user.role == User.Role.TENANT_ADMIN:
            raise serializers.ValidationError(
                "Tenant administrators must log in on the management host."
            )
        return data