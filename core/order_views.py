from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import ApiError
from .models import IdempotencyRecord, Order
from .orders import (
    apply_action,
    create_draft,
    load_order,
    order_queryset,
    parse_dealer,
    parse_items,
    replace_items,
    request_hash,
    serialize_order,
)
from .pagination import StandardPagination


def json_body(request):
    if not isinstance(request.data, dict):
        raise ValidationError({"body": ["Body must be a JSON object."]})
    return request.data


class OrderListCreate(APIView):
    def get(self, request):
        qs = order_queryset().order_by("-id")
        dealer_id = request.query_params.get("dealer_id")
        order_status = request.query_params.get("status")
        if dealer_id:
            if not dealer_id.isdigit():
                raise ValidationError({"dealer_id": ["Must be a whole number."]})
            qs = qs.filter(dealer_id=int(dealer_id))
        if order_status:
            qs = qs.filter(status=order_status)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response([serialize_order(o) for o in page])

    def post(self, request):
        body = json_body(request)
        key = request.headers.get("Idempotency-Key")
        body_hash = request_hash(body)

        if key:
            existing = IdempotencyRecord.objects.filter(key=key).first()
            if existing:
                return self.replay(existing, body_hash)

        dealer = parse_dealer(body.get("dealer_id"))
        lines = parse_items(body.get("items"))

        try:
            with transaction.atomic():
                record = None
                if key:
                    # The unique key is the real guard. If two identical requests
                    # arrive together, the second one waits here, then fails.
                    record = IdempotencyRecord.objects.create(key=key, request_hash=body_hash)
                order = create_draft(dealer, lines)
                if record:
                    record.order = order
                    record.save(update_fields=["order"])
        except IntegrityError:
            existing = IdempotencyRecord.objects.filter(key=key).first() if key else None
            if existing is None:
                raise
            return self.replay(existing, body_hash)

        return Response(serialize_order(load_order(order.order_number)), status=201)

    def replay(self, record, body_hash):
        if record.request_hash != body_hash:
            raise ApiError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This Idempotency-Key was already used with a different request body.",
            )
        return Response(serialize_order(load_order(record.order.order_number)), status=200)


class OrderDetail(APIView):
    def get(self, request, order_number):
        order = get_object_or_404(order_queryset(), order_number=order_number)
        return Response(serialize_order(order))

    def put(self, request, order_number):
        get_object_or_404(Order, order_number=order_number)
        body = json_body(request)
        lines = parse_items(body.get("items"))
        replace_items(order_number, lines)
        return Response(serialize_order(load_order(order_number)))
class OrderAction(APIView):
    action = None  # set in urls.py

    def post(self, request, order_number):
        apply_action(order_number, self.action)
        return Response(serialize_order(load_order(order_number)))