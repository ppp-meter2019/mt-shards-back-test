from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from users.models import User

from .models import Customer


class CustomerSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username")
    password = serializers.CharField(write_only=True, required=False)
    first_name = serializers.CharField(source="user.first_name", required=False, allow_blank=True)
    last_name = serializers.CharField(source="user.last_name", required=False, allow_blank=True)
    email = serializers.EmailField(source="user.email", required=False, allow_blank=True)

    class Meta:
        model = Customer
        fields = [
            "id",
            "username",
            "password",
            "first_name",
            "last_name",
            "email",
            "phone",
            "address",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_username(self, value: str) -> str:
        qs = User.objects.filter(username=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.user_id)
        if qs.exists():
            raise serializers.ValidationError("This login is already in use.")
        return value

    def validate(self, attrs):
        if "password" in attrs:
            validate_password(attrs["password"])
        elif not self.instance:
            raise serializers.ValidationError({"password": "Password is required."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        user_data = validated_data.pop("user")
        password = validated_data.pop("password")
        user = User.objects.create_user(
            password=password,
            role=User.Role.CUSTOMER,
            **user_data,
        )
        return Customer.objects.create(user=user, **validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", {})
        password = validated_data.pop("password", None)
        if user_data or password:
            user = instance.user
            for field, value in user_data.items():
                setattr(user, field, value)
            if password:
                user.set_password(password)
            user.save()
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        return instance