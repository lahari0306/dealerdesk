from decimal import Decimal

from rest_framework import serializers

from .models import Dealer, Product


class ProductSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )
    stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "sku", "name", "category", "brand",
            "vehicle_fitment", "price", "stock", "description",
        ]
        # No automatic "unique" check: the database decides, and we return 409.
        extra_kwargs = {"sku": {"validators": []}}

    def get_stock(self, obj):
        inventory = getattr(obj, "inventory", None)
        return inventory.quantity if inventory else 0


class DealerSerializer(serializers.ModelSerializer):
    credit_limit = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False
    )

    class Meta:
        model = Dealer
        fields = [
            "id", "dealer_code", "name", "city",
            "phone", "email", "gstin", "credit_limit",
        ]
        extra_kwargs = {"dealer_code": {"validators": []}}