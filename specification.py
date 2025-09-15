from rest_framework import serializers

from inventory2.backend.models.specification import Specification


class SpecificationsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Specification
        fields = "__all__"
