from rest_framework import serializers

from .models import Car


class CarSerializer(serializers.ModelSerializer):
    class Meta:
        model = Car
        fields = ["id", "brand", "model", "year", "license_plate", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_license_plate(self, value: str) -> str:
        return value.upper().strip()