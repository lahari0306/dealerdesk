# DECISIONS (Django track)

## 1. Where exactly is overselling prevented?
`core/orders.py:218-243` (`apply_action`) runs in `transaction.atomic`. Line 225 locks the order row with `select_for_update()`. For a confirm, line 237 calls `take_stock` (`orders.py:176-204`), which calls `lock_inventory` (`orders.py:165-173`). That locks the Inventory rows with `select_for_update()`, sorted by `product_id`. The stock check (lines 180-197) and the deduction (lines 199-204) both happen while those row locks are held.

Without the lock, two confirms arriving together would both read "5 available", both pass the check, and both subtract, so stock goes negative and two dealers are promised the same units. With the lock, the second request waits until the first commits, then reads the reduced quantity and gets `409 INSUFFICIENT_STOCK`. Sorting by `product_id` means two orders always lock rows in the same order, so they cannot deadlock.

## 2. How do you calculate and round GST?
`core/orders.py:13-19` defines `GST_RATE = Decimal("0.18")` and `money()`, which uses `quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`. In `serialize_order` (`orders.py:127-128`), the line subtotal is `money(unit_price * quantity)` and the line tax is `money(line_subtotal * GST_RATE)`. Tax is rounded per line, then the lines are summed (`orders.py:129-130`). Totals are recomputed from the lines on every read, so they cannot go stale.

Floats cannot store values like 0.1 exactly, so sums drift by a paisa. Decimal's default rounding is `ROUND_HALF_EVEN`, so 0.045 would become 0.04 instead of the required 0.05.

## 3. Unique order numbers and idempotent creation under simultaneous requests
**Order numbers:** `next_order_number()` (`orders.py:76-84`) keeps one row per IST day in `OrderSequence` (`models.py:71-75`, `date` is unique). Line 79 creates the row if missing. Line 81 reads it with `select_for_update()`, so parallel requests queue up. Line 82 increments the counter before the number is formatted on line 84. It is called inside `create_draft`, which is `@transaction.atomic` (`orders.py:87-89`). Because the lock is held until commit, two orders can never receive the same number. `Order.order_number` is also `unique=True` (`models.py:46`) as a second guard.

**Idempotency:** `OrderListCreate.post` (`order_views.py:44-74`). `IdempotencyRecord.key` is unique at the database level (`models.py:79`). Line 63 inserts the record inside the same transaction as the order (lines 58-67). If two identical requests arrive together, the second insert waits for the first to commit, then fails with `IntegrityError`. The `except` at lines 68-72 catches it and replays the stored order instead of creating a second one. The pre-check at lines 49-52 is only a fast path; the unique constraint is what actually guarantees one order. `replay` (`order_views.py:76-83`) compares the SHA-256 hash of the body (`orders.py:26-28`) and returns 200 with the same order, or `409 IDEMPOTENCY_CONFLICT` if the same key arrives with a different body. Because the record and the order are saved in one transaction, a failed order leaves no orphan record.

## 4. Schema and `on_delete` choices (`core/models.py`)
- **Product** (line 4): `sku` unique, `price` is a `DecimalField`, `category` is indexed.
- **Inventory** (line 17): one-to-one with Product, `CASCADE` (line 19). Stock has no meaning without its product.
- **Dealer** (line 27): `dealer_code` unique.
- **Order** (line 38): `dealer` is a ForeignKey with `PROTECT` (line 47), so a dealer who has orders cannot be deleted and history stays intact. `status` is indexed. `order_number` is unique.
- **OrderItem** (line 58): `order` uses `CASCADE` (line 59), because lines belong to their order. `product` uses `PROTECT` (line 60), so a product that appears in an order cannot be deleted. `unit_price` stores the price at confirm time. A unique constraint on `(order, product)` (line 67) stops duplicate lines.
- **OrderSequence** (line 71): the per-day counter, `date` unique.
- **IdempotencyRecord** (line 78): `key` unique. `order` is a ForeignKey with `CASCADE` and `null=True` (line 81), so the record can be created before the order exists and is removed with the order.

## 5. Where are status transitions enforced, and how does cancel restore stock exactly once?
Transitions are defined in the `ACTIONS` table (`orders.py:157-162`) and enforced in `apply_action` (`orders.py:229-234`): if the current status is not in the allowed set, it raises `409 INVALID_TRANSITION`. Views only call `apply_action` (`order_views.py:97-101`), so there is no other path that changes status.

Line 225 locks the order row first. A second cancel for the same order waits there, then sees the new status `CANCELLED` and is rejected at line 229, so line 239 (`give_stock_back`, `orders.py:207-215`) runs only once. Cancelling a DRAFT never touches stock (line 238 only restores for CONFIRMED). The status change and the stock change are in the same transaction, so they succeed or fail together.

## 6. Supplier sync: rate limits, failures, duplicates, and changing nothing on failure
`fetch_page` (`core/sync.py:24-63`) tries each request at most 5 times (`MAX_ATTEMPTS`, line 14). On `429` it sleeps for `Retry-After`, capped at 20 seconds (lines 48-53). On `5xx` or a connection error it sleeps a short backoff (lines 34, 54-55, 60-61). On `401` or any other client error it fails immediately with `502 SUPPLIER_UNAVAILABLE` (lines 56-58), because retrying cannot help. After 5 failed attempts it raises the same 502 (line 63). `fetch_everything` (`sync.py:66-78`) follows `next_cursor` through every page and stops with a 502 if a cursor repeats (line 76).

All pages are downloaded in memory at line 108, and the database is not touched until that succeeds. The writes happen in one `transaction.atomic()` block (line 133), so a fetch failure changes nothing, and a database error rolls everything back. Invalid items (missing sku, bad price) are skipped and reported in `rejected` (lines 113-124). Duplicate SKUs are resolved by keeping the item with the newest `updated_at` (lines 126-129).

## 7. How does the sync decide created, updated or unchanged, and why does a second run change nothing?
`run_sync` loads existing products for the SKUs in one query (`sync.py:134`). A SKU not in the database goes to `to_create` (lines 141-143). Otherwise each supplier field is compared with the database value (line 144). If any field differs, only those fields are set and the product goes to `to_update` (lines 145-148). If nothing differs, it counts as unchanged (line 150). New products get an Inventory row with quantity 0 (lines 154-156). `bulk_update` only touches product fields (`UPDATE_FIELDS`, line 157), so the stock of existing products is never changed.

On a second run the supplier values equal what is already stored (price is compared as `Decimal`, text as `str`), so every product is unchanged, and `created` and `updated` are both 0.

## 8. Weakest part, and what I would do with two more days
The weakest part is verification. The only automated tests are a small set in `core/tests.py` (GST rounding, stock, cancel, price lock, idempotency). I have not written a true concurrency test that fires many confirms at the same moment with threads. `seed_data` (`core/management/commands/seed_data.py:19-34`) also uses `update_or_create` and `get_or_create` row by row, which issues several queries per product (a few thousand in total) and is slow on a remote database. With two more days I would add a threaded concurrency test for confirm and idempotent create, convert the seed to `bulk_create` with conflict handling, add sync tests against the mock supplier (rate limits, outage, duplicates), and add logging. I did not attempt the optional parts (assistant, forecasting, frontend).
