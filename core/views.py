from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import (
    ListCreateAPIView,
    RetrieveAPIView,
    RetrieveUpdateAPIView,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import ApiError
from .models import Dealer, Inventory, Product
from .serializers import DealerSerializer, ProductSerializer


def health(request):
    """GET /api/health/ — the grader polls this until your app is ready."""
    return JsonResponse({"status": "ok"})


class ProductListCreate(ListCreateAPIView):
    serializer_class = ProductSerializer

    def get_queryset(self):
        qs = Product.objects.select_related("inventory").order_by("sku")
        params = self.request.query_params
        search = params.get("search")
        category = params.get("category")
        vehicle = params.get("vehicle")
        if search:
            qs = qs.filter(
                Q(sku__icontains=search) | Q(name__icontains=search) | Q(brand__icontains=search)
            )
        if category:
            qs = qs.filter(category=category)
        if vehicle:
            qs = qs.filter(vehicle_fitment__icontains=vehicle)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                product = serializer.save()
                Inventory.objects.create(product=product, quantity=0)
        except IntegrityError:
            raise ApiError(409, "DUPLICATE", "A product with this SKU already exists.")
        product = Product.objects.select_related("inventory").get(pk=product.pk)
        return Response(ProductSerializer(product).data, status=status.HTTP_201_CREATED)


class ProductDetail(RetrieveUpdateAPIView):
    queryset = Product.objects.select_related("inventory")
    serializer_class = ProductSerializer
    lookup_field = "sku"
    http_method_names = ["get", "patch", "head", "options"]

    def partial_update(self, request, *args, **kwargs):
        product = self.get_object()
        new_sku = request.data.get("sku")
        if new_sku is not None and new_sku != product.sku:
            raise ValidationError({"sku": "SKU cannot be changed."})
        return super().partial_update(request, *args, **kwargs)


class InventoryUpdate(APIView):
    """PUT /api/inventory/{sku}/ sets the stock to exactly this number."""

    def put(self, request, sku):
        product = get_object_or_404(Product, sku=sku)
        quantity = request.data.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValidationError({"quantity": "Must be an integer of 0 or more."})

        with transaction.atomic():
            # select_for_update locks this stock row until the transaction ends.
            inventory, _ = Inventory.objects.select_for_update().get_or_create(product=product)
            inventory.quantity = quantity
            inventory.save(update_fields=["quantity"])
        return Response({"sku": product.sku, "quantity": inventory.quantity})


class DealerListCreate(ListCreateAPIView):
    queryset = Dealer.objects.order_by("id")
    serializer_class = DealerSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                dealer = serializer.save()
        except IntegrityError:
            raise ApiError(409, "DUPLICATE", "A dealer with this dealer_code already exists.")
        return Response(DealerSerializer(dealer).data, status=status.HTTP_201_CREATED)


class DealerDetail(RetrieveAPIView):
    queryset = Dealer.objects.all()
    serializer_class = DealerSerializer