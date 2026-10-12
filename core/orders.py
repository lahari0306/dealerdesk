import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .errors import ApiError
from .models import Dealer, Inventory, Order, OrderItem, OrderSequence, Product

CENT = Decimal("0.01")
GST_RATE = Decimal("0.18")


def money(value):
    """Round to 2 decimals, with 0.005 going UP to 0.01."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value):
    return f"{value:.2f}"


def request_hash(body):
    text = json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def parse_dealer(dealer_id):
    if isinstance(dealer_id, bool) or not isinstance(dealer_id, int):
        raise ValidationError({"dealer_id": ["dealer_id must be a whole number."]})
    dealer = Dealer.objects.filter(pk=dealer_id).first()
    if dealer is None:
        raise ValidationError({"dealer_id": ["Unknown dealer."]})
    return dealer


def parse_items(raw_items):
    """Checks the items list. Returns [(Product, quantity), ...]."""
    if not isinstance(raw_items, list) or not raw_items:
        raise ValidationError({"items": ["At least one item is required."]})

    problems, seen, wanted = [], set(), []
    for i, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            problems.append(f"items[{i}] must be an object.")
            continue
        sku, qty = raw.get("sku"), raw.get("quantity")
        if not isinstance(sku, str) or not sku.strip():
            problems.append(f"items[{i}]: sku is required.")
            continue
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            problems.append(f"items[{i}]: quantity must be a whole number of 1 or more.")
            continue
        if sku in seen:
            problems.append(f"items[{i}]: sku {sku} appears more than once.")
            continue
        seen.add(sku)
        wanted.append((sku, qty))

    products = {p.sku: p for p in Product.objects.filter(sku__in=[s for s, _ in wanted])}
    lines = []
    for sku, qty in wanted:
        if sku not in products:
            problems.append(f"Unknown SKU: {sku}.")
        else:
            lines.append((products[sku], qty))

    if problems:
        raise ValidationError({"items": problems})
    return lines


def next_order_number():
    """ORD-YYYYMMDD-NNNN using the IST date. Must run inside a transaction."""
    today = timezone.localdate()  # TIME_ZONE is Asia/Kolkata
    OrderSequence.objects.get_or_create(date=today)
    # The row lock makes parallel requests wait in a line, so numbers never repeat.
    seq = OrderSequence.objects.select_for_update().get(date=today)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"ORD-{today:%Y%m%d}-{seq.last_number:04d}"


@transaction.atomic
def create_draft(dealer, lines):
    order = Order.objects.create(order_number=next_order_number(), dealer=dealer)
    OrderItem.objects.bulk_create(
        [OrderItem(order=order, product=p, quantity=q, unit_price=p.price) for p, q in lines]
    )
    return order


@transaction.atomic
def replace_items(order_number, lines):
    order = Order.objects.select_for_update().filter(order_number=order_number).first()
    if order is None:
        raise Http404
    if order.status != Order.Status.DRAFT:
        raise ApiError(409, "ORDER_LOCKED", "Only DRAFT orders can be edited.")
    order.items.all().delete()
    OrderItem.objects.bulk_create(
        [OrderItem(order=order, product=p, quantity=q, unit_price=p.price) for p, q in lines]
    )
    order.save(update_fields=["updated_at"])
    return order


def order_queryset():
    return Order.objects.select_related("dealer").prefetch_related("items__product")


def load_order(order_number):
    return order_queryset().filter(order_number=order_number).first()


def serialize_order(order):
    """Totals are worked out from the lines every time, so they can never go stale."""
    is_draft = order.status == Order.Status.DRAFT
    items = []
    subtotal = tax_total = Decimal("0.00")
    for item in order.items.all():
        # A DRAFT follows today's catalogue price. Other orders use the locked price.
        unit_price = item.product.price if is_draft else item.unit_price
        line_subtotal = money(unit_price * item.quantity)
        line_tax = money(line_subtotal * GST_RATE)  # tax is rounded PER LINE
        subtotal += line_subtotal
        tax_total += line_tax
        items.append(
            {
                "sku": item.product.sku,
                "name": item.product.name,
                "quantity": item.quantity,
                "unit_price": fmt(unit_price),
                "line_subtotal": fmt(line_subtotal),
                "line_tax": fmt(line_tax),
                "line_total": fmt(line_subtotal + line_tax),
            }
        )
    return {
        "order_number": order.order_number,
        "dealer_id": order.dealer_id,
        "status": order.status,
        "items": items,
        "subtotal": fmt(subtotal),
        "tax_total": fmt(tax_total),
        "grand_total": fmt(subtotal + tax_total),
        "created_at": timezone.localtime(order.created_at).isoformat(),
        "updated_at": timezone.localtime(order.updated_at).isoformat(),
    }


# ---- Status changes and stock ---------------------------------------------------

ACTIONS = {
    "confirm": ({Order.Status.DRAFT}, Order.Status.CONFIRMED),
    "dispatch": ({Order.Status.CONFIRMED}, Order.Status.DISPATCHED),
    "deliver": ({Order.Status.DISPATCHED}, Order.Status.DELIVERED),
    "cancel": ({Order.Status.DRAFT, Order.Status.CONFIRMED}, Order.Status.CANCELLED),
}


def lock_inventory(product_ids):
    """Locks the stock rows. Sorted by id so two orders can never lock in opposite
    order and freeze each other (a deadlock)."""
    rows = (
        Inventory.objects.select_for_update()
        .filter(product_id__in=product_ids)
        .order_by("product_id")
    )
    return {row.product_id: row for row in rows}


def take_stock(order):
    items = list(order.items.select_related("product").order_by("product_id"))
    stock = lock_inventory([i.product_id for i in items])

    short = []
    for item in items:
        row = stock.get(item.product_id)
        available = row.quantity if row else 0
        if item.quantity > available:
            short.append((item, available))

    if short:  # all or nothing: nothing has been changed yet
        message = " ".join(
            f"Insufficient stock for {item.product.name}. "
            f"Available: {available}, Requested: {item.quantity}"
            for item, available in short
        )
        details = [
            {"sku": item.product.sku, "requested": item.quantity, "available": available}
            for item, available in short
        ]
        raise ApiError(409, "INSUFFICIENT_STOCK", message, details)

    for item in items:
        row = stock[item.product_id]
        row.quantity -= item.quantity
        row.save(update_fields=["quantity"])
        item.unit_price = item.product.price  # price is locked from now on
        item.save(update_fields=["unit_price"])


def give_stock_back(order):
    items = list(order.items.order_by("product_id"))
    stock = lock_inventory([i.product_id for i in items])
    for item in items:
        row = stock.get(item.product_id)
        if row is None:
            row = Inventory.objects.create(product_id=item.product_id, quantity=0)
        row.quantity += item.quantity
        row.save(update_fields=["quantity"])


@transaction.atomic
def apply_action(order_number, action):
    allowed_from, new_status = ACTIONS[action]

    # Lock the order first. A second request for the same order waits here,
    # and when it gets in, it sees the NEW status. That is what makes a
    # double cancel restore the stock only once.
    order = Order.objects.select_for_update().filter(order_number=order_number).first()
    if order is None:
        raise Http404

    if order.status not in allowed_from:
        raise ApiError(
            409,
            "INVALID_TRANSITION",
            f"Cannot {action} an order that is {order.status}.",
        )

    if action == "confirm":
        take_stock(order)
    elif action == "cancel" and order.status == Order.Status.CONFIRMED:
        give_stock_back(order)

    order.status = new_status
    order.save(update_fields=["status", "updated_at"])
    return order