import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import ApiError
from .models import Inventory, Product

MAX_ATTEMPTS = 5          # per request, as the contract says
MAX_WAIT = 20             # never sleep longer than this for one Retry-After
TEXT_FIELDS = ["name", "category", "brand", "vehicle_fitment", "description"]
UPDATE_FIELDS = TEXT_FIELDS + ["price"]


def supplier_error(message):
    return ApiError(502, "SUPPLIER_UNAVAILABLE", message)


def fetch_page(session, cursor, counter):
    """Gets one page. Tries at most 5 times. Raises 502 if it never works."""
    url = settings.SUPPLIER_API_URL.rstrip("/") + "/v1/products"
    params = {"limit": 20}
    if cursor:
        params["cursor"] = cursor
    headers = {"X-Api-Key": settings.SUPPLIER_API_KEY}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        counter["requests"] += 1
        wait = 0.5 * attempt  # short backoff for 5xx and connection errors
        try:
            resp = session.get(url, params=params, headers=headers, timeout=10)
        except requests.RequestException:
            resp = None

        if resp is not None:
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and isinstance(body.get("items"), list):
                    return body
            elif resp.status_code == 429:
                try:
                    wait = float(resp.headers.get("Retry-After", 1))
                except ValueError:
                    wait = 1
                wait = min(max(wait, 0), MAX_WAIT)
            elif resp.status_code >= 500:
                pass  # temporary failure: retry after the short backoff
            else:
                # 401 (key rejected) or any other client error: retrying won't help
                raise supplier_error(f"Supplier rejected the request ({resp.status_code}).")

        if attempt < MAX_ATTEMPTS:
            time.sleep(wait)

    raise supplier_error("Supplier did not respond after 5 attempts.")


def fetch_everything(counter):
    items, cursor, pages, seen = [], None, 0, set()
    with requests.Session() as session:
        while True:
            body = fetch_page(session, cursor, counter)
            pages += 1
            items.extend(body["items"])
            cursor = body.get("next_cursor")
            if not cursor:
                return items, pages
            if cursor in seen:  # a repeating cursor would loop forever
                raise supplier_error("Supplier returned a repeating cursor.")
            seen.add(cursor)


def parse_price(value):
    """A positive amount with at most 2 decimals, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0 or price.as_tuple().exponent < -2:
        return None
    return price.quantize(Decimal("0.01"))


def parse_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)  # unreadable = oldest
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def run_sync():
    counter = {"requests": 0}
    # 1. Download EVERYTHING first. The database is not touched until this succeeds,
    #    so a failed download changes nothing.
    items, pages = fetch_everything(counter)

    # 2. Drop invalid items, then keep the newest version of each SKU.
    rejected, newest = [], {}
    for item in items:
        if not isinstance(item, dict):
            rejected.append({"supplier_sku": None, "reason": "invalid item"})
            continue
        supplier_sku = item.get("supplier_sku")
        sku = item.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            rejected.append({"supplier_sku": supplier_sku, "reason": "missing sku"})
            continue
        price = parse_price(item.get("price"))
        if price is None:
            rejected.append({"supplier_sku": supplier_sku, "reason": "invalid price"})
            continue
        sku = sku.strip()
        stamp = parse_time(item.get("updated_at"))
        current = newest.get(sku)
        if current is None or stamp >= current[0]:
            newest[sku] = (stamp, item, price)

    # 3. Write to the database in one transaction.
    created = updated = unchanged = 0
    with transaction.atomic():
        existing = {p.sku: p for p in Product.objects.filter(sku__in=list(newest))}
        to_create, to_update = [], []

        for sku, (_, item, price) in newest.items():
            values = {f: str(item[f]) for f in TEXT_FIELDS if item.get(f) is not None}
            values["price"] = price
            product = existing.get(sku)
            if product is None:
                to_create.append(Product(sku=sku, **values))
                continue
            changed = [f for f, v in values.items() if getattr(product, f) != v]
            if changed:
                for f in changed:
                    setattr(product, f, values[f])
                to_update.append(product)
            else:
                unchanged += 1

        new_products = Product.objects.bulk_create(to_create)
        # New products start with stock 0. We never touch stock of existing ones.
        Inventory.objects.bulk_create(
            [Inventory(product=p, quantity=0) for p in new_products]
        )
        Product.objects.bulk_update(to_update, UPDATE_FIELDS, batch_size=500)
        created, updated = len(to_create), len(to_update)

    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "rejected": rejected,
        "pages_fetched": pages,
        "requests_made": counter["requests"],
    }


class SupplierSync(APIView):
    def post(self, request):
        return Response(run_sync())