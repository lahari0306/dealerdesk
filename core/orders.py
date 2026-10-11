
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F
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
            problems.append(
                f"items[{i}]: quantity must be a whole number of 1 or more."
            )
            continue

        if sku in seen:
            problems.append(f"items[{i}]: sku {sku} appears more than once.")
            continue

        seen.add(sku)
        wanted.append((sku, qty))

    products = {
        p.sku: p
        for p in Product.objects.filter(sku__in=[s for s, _ in wanted])
    }

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
    """Generate a daily order number. Must run inside a transaction."""
    today = timezone.localdate()
    OrderSequence.objects.get_or_create(date=today)
    seq = OrderSequence.objects.select_for_update().get(date=today)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return f"ORD-{today:%Y%m%d}-{seq.last_number:04d}"


@transaction.atomic
def create_draft(dealer, lines):
    order = Order.objects.create(
        order_number=next_order_number(),
        dealer=dealer,
    )
    OrderItem.objects.bulk_create([
        OrderItem(
            order=order,
            product=product,
            quantity=quantity,
            unit_price=product.price,
        )
        for product, quantity in lines
    ])
    return order


@transaction.atomic
def replace_items(order_number, lines):
    order = Order.objects.select_for_update().filter(
        order_number=order_number
    ).first()

    if order is None:
        raise Http404

    if order.status != Order.Status.DRAFT:
        raise ApiError(
            409, "ORDER_LOCKED", "Only DRAFT orders can be edited."
        )

    order.items.all().delete()
    OrderItem.objects.bulk_create([
        OrderItem(
            order=order,
            product=product,
            quantity=quantity,
            unit_price=product.price,
        )
        for product, quantity in lines
    ])

    order.save(update_fields=["updated_at"])
    return order


def order_queryset():
    return Order.objects.select_related(
        "dealer"
    ).prefetch_related("items__product")


def load_order(order_number):
    return order_queryset().filter(order_number=order_number).first()


def serialize_order(order):
    """Calculate order totals from the order lines."""
    is_draft = order.status == Order.Status.DRAFT
    items = []
    subtotal = tax_total = Decimal("0.00")

    for item in order.items.all():
        unit_price = item.product.price if is_draft else item.unit_price
        line_subtotal = money(unit_price * item.quantity)
        line_tax = money(line_subtotal * GST_RATE)

        subtotal += line_subtotal
        tax_total += line_tax

        items.append({
            "sku": item.product.sku,
            "name": item.product.name,
            "quantity": item.quantity,
            "unit_price": fmt(unit_price),
            "line_subtotal": fmt(line_subtotal),
            "line_tax": fmt(line_tax),
            "line_total": fmt(line_subtotal + line_tax),
        })

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


# Order actions: DRAFT -> CONFIRMED -> DISPATCHED -> DELIVERED
# A DRAFT or CONFIRMED order may be cancelled.
ACTIONS = {
    "confirm": {
        "from": Order.Status.DRAFT,
        "to": Order.Status.CONFIRMED,
    },
    "dispatch": {
        "from": Order.Status.CONFIRMED,
        "to": Order.Status.DISPATCHED,
    },
    "deliver": {
        "from": Order.Status.DISPATCHED,
        "to": Order.Status.DELIVERED,
    },
    "cancel": {
        "from": (Order.Status.DRAFT, Order.Status.CONFIRMED),
        "to": Order.Status.CANCELLED,
    },
}


def lock_inventory(order):
    """Lock inventory rows in a consistent order."""
    product_ids = list(
        order.items.order_by("product_id").values_list(
            "product_id", flat=True
        )
    )

    inventory = list(
        Inventory.objects.select_for_update()
        .filter(product_id__in=product_ids)
        .order_by("product_id")
    )

    by_product = {row.product_id: row for row in inventory}

    missing = [
        str(product_id)
        for product_id in product_ids
        if product_id not in by_product
    ]

    if missing:
        raise ApiError(
            409,
            "INVENTORY_MISSING",
            f"Inventory is missing for product IDs: {', '.join(missing)}.",
        )

    return by_product


def take_stock(order, inventory):
    """Deduct stock when an order is confirmed."""
    for item in order.items.all().order_by("product_id"):
        stock = inventory[item.product_id]

        if stock.quantity < item.quantity:
            raise ApiError(
                409,
                "INSUFFICIENT_STOCK",
                f"Insufficient stock for SKU {item.product.sku}.",
            )

    for item in order.items.all().order_by("product_id"):
        Inventory.objects.filter(pk=inventory[item.product_id].pk).update(
            quantity=F("quantity") - item.quantity
        )


def give_stock_back(order, inventory):
    """Restore stock when a confirmed order is cancelled."""
    for item in order.items.all().order_by("product_id"):
        Inventory.objects.filter(pk=inventory[item.product_id].pk).update(
            quantity=F("quantity") + item.quantity
        )


@transaction.atomic
def apply_action(order_number, action):
    """Apply an allowed order action atomically."""
    if action not in ACTIONS:
        raise ValidationError({
            "action": [f"Unknown action: {action}."]
        })

    order = Order.objects.select_for_update().filter(
        order_number=order_number
    ).first()

    if order is None:
        raise Http404

    rule = ACTIONS[action]
    allowed_from = rule["from"]

    if order.status not in (
        allowed_from if isinstance(allowed_from, tuple) else (allowed_from,)
    ):
        raise ApiError(
            409,
            "",
            f"Action '{action}' is not allowed when order status is "
            f"{order.status}.",
        )

    if action == "confirm":
        inventory = lock_inventory(order)
        take_stock(order, inventory)

    elif action == "cancel" and order.status == Order.Status.CONFIRMED:
        inventory = lock_inventory(order)
        give_stock_back(order, inventory)

    order.status = rule["to"]
    order.save(update_fields=["status", "updated_at"])

    return order
