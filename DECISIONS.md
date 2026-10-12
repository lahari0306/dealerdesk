# DECISIONS

## 1. Where is overselling prevented?
core/orders.py:218-243 (`apply_action`) runs inside `transaction.atomic`. It locks the
order row (line 225), then `take_stock` (line 176) calls `lock_inventory` (lines 165-173),
which uses `select_for_update()` on the Inventory rows, sorted by product_id. The stock
check (lines 180-197) and the deduction (lines 199-204) happen while those locks are held.
Without the lock, two confirms arriving together would both read "5 available", both pass the
check, and both deduct, so stock would go negative and two dealers would be promised the same
units. With the lock, the second request waits, then reads the reduced stock and gets
409 INSUFFICIENT_STOCK. Sorting by product_id means two orders always lock rows in the same
order, so they cannot deadlock.

## 2. GST and rounding
core/orders.py:13-19 and the loop in `serialize_order`. Money uses `Decimal`, never float.
`money()` rounds with `ROUND_HALF_UP`. Tax is rounded per line (line_subtotal * 0.18), then
summed. Floats cannot represent values like 0.1 exactly, so totals drift by a paisa.
Decimal's default rounding is ROUND_HALF_EVEN, which turns 0.005 into 0.00 instead of 0.01.

## 3. How are duplicate order requests handled?

`core/order_views.py`, `OrderListCreate.post()` and `replay()` implement idempotency using the `Idempotency-Key` request header. The request body is hashed using `request_hash()`. If a key already exists with the same request hash, the existing order is returned with HTTP 200 instead of creating a duplicate. If the same key is reused with a different body, the API returns HTTP 409 with `IDEMPOTENCY_CONFLICT`.

For concurrent requests, the code creates an `IdempotencyRecord` inside `transaction.atomic()`. The database unique constraint on the key ensures that only one record can be created for a given key. If a concurrent request violates this constraint, `IntegrityError` is caught and the existing record is replayed. The record and order are created in the same transaction, so a failed order creation also rolls back the idempotency record.

## 4. Explain your schema and relationships

`core/models.py` defines the database schema.

- **Product:** Stores SKU, name, category, brand, vehicle fitment, description and decimal price. SKU is unique to prevent duplicate product identifiers.
- **Inventory:** Has a one-to-one relationship with Product. `CASCADE` deletes inventory when its product is deleted because that stock record is no longer needed.
- **Dealer:** Stores dealer details, unique dealer code and decimal credit limit.
- **Order:** Belongs to a Dealer through a foreign key using `PROTECT`. This prevents deleting a dealer who has orders.
- **OrderItem:** Belongs to an Order using `CASCADE`, so deleting an order deletes its line items. Its Product foreign key uses `PROTECT`, preventing deletion of products referenced by order history. A unique constraint on `(order, product)` prevents duplicate product lines in an order.
- **OrderSequence:** Stores the last order number used for each date. Its unique date constraint supports unique daily order-number generation.
- **IdempotencyRecord:** Stores a unique request key, request hash and associated order. Its order foreign key uses `CASCADE` with `null=True`, so deleting the order also deletes its idempotency record.

These relationships preserve referential integrity, protect order history from accidental deletion, and keep related records consistent.


## 5. Where are status transitions enforced, and how is stock restored exactly once?

`core/orders.py`, lines containing `ACTIONS` and `apply_action()`, define and enforce valid status transitions. The allowed flow is DRAFT → CONFIRMED → DISPATCHED → DELIVERED. DRAFT and CONFIRMED orders can be cancelled. Any other transition raises `ApiError` with HTTP 409 and `INVALID_TRANSITION`.

`apply_action()` runs inside `transaction.atomic()` and locks the order row using `select_for_update()`. When confirming, `take_stock()` locks inventory rows in product ID order, checks all requested quantities before deducting anything, and raises `INSUFFICIENT_STOCK` if stock is insufficient. When cancelling a CONFIRMED order, `give_stock_back()` restores the quantities. The order lock ensures a second concurrent cancellation waits, then sees CANCELLED and is rejected, preventing stock from being restored twice. The transaction keeps stock changes and the status update together.

## 6. How does supplier sync handle rate limits, failures, duplicates, and atomicity?

`core/sync.py`, `fetch_page()` retries failed requests up to 5 times. For HTTP 429, it respects the `Retry-After` header, capped at 20 seconds. Server errors and request exceptions use a short backoff. Unrecoverable client errors raise `SUPPLIER_UNAVAILABLE` with HTTP 502. `fetch_everything()` downloads every page before any database writes and rejects repeating cursors to avoid infinite loops. Therefore, a fetch failure leaves the database unchanged. After fetching, `run_sync()` validates items and prices, keeps the newest version for each SKU, and performs all database writes inside `transaction.atomic()`, so a database failure rolls back the sync changes.


## 7. How does sync decide created, updated, and unchanged?

`core/sync.py`, `run_sync()`, compares each valid supplier product with the existing product having the same SKU. A missing SKU is created with its supplied fields and price, and a new Inventory record is initialized with quantity 0. If an existing product has changed values, only those fields are updated and the product is counted as updated. If no compared fields differ, it is counted as unchanged. Duplicate supplier entries for the same SKU are reduced to the entry with the newest `updated_at` timestamp before writing. Because unchanged products are not saved, and existing inventory quantities are never modified, a second identical sync should report existing products as unchanged and leave their stock untouched.


## 8. What is the weakest part of my submission, and what would I improve with two more days?

The weakest part of my submission is the limited verification of edge cases beyond the supplied smoke test. With two more days, I would add automated tests for concurrent order confirmations, simultaneous idempotent requests, cancellation and stock restoration, supplier rate limits and outages, duplicate supplier records, and repeated syncs. I would also improve logging and document the setup and testing process so the application is easier to maintain and verify.