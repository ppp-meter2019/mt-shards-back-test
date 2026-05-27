import datetime

from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from rest_framework import serializers

from users.models import User

from .models import Driver


MIN_DRIVER_AGE = 18


class DriverSerializer(serializers.ModelSerializer):
    """Flat representation: caller posts user + driver fields together."""

    username = serializers.CharField(source="user.username")
    password = serializers.CharField(write_only=True, required=False, allow_blank=False)
    first_name = serializers.CharField(source="user.first_name")
    last_name = serializers.CharField(source="user.last_name")

    class Meta:
        model = Driver
        fields = [
            "id",
            "username",
            "password",
            "first_name",
            "last_name",
            "date_of_birth",
            "license_number",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_license_number(self, value: str) -> str:
        return value.upper().strip()

    def validate_date_of_birth(self, value: datetime.date) -> datetime.date:
        today = datetime.date.today()
        if value > today:
            raise serializers.ValidationError("Date of birth cannot be in the future.")
        # crude age check — month/day comparison
        age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
        if age < MIN_DRIVER_AGE:
            raise serializers.ValidationError(
                f"Driver must be at least {MIN_DRIVER_AGE} years old."
            )
        return value

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
            username=user_data["username"],
            password=password,
            first_name=user_data.get("first_name", ""),
            last_name=user_data.get("last_name", ""),
            role=User.Role.DRIVER,
        )
        return Driver.objects.create(user=user, **validated_data)

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