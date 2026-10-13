# DealerDesk

**Track: django**

Dealer ordering backend (Django + DRF + PostgreSQL) with a strict order engine and a supplier sync.

## What is built
- A1 Backend core: products, inventory, dealers, draft orders, one error format
- A2 Order engine: status flow, no overselling, exact GST, order numbers, idempotency
- B Supplier sync: `POST /api/sync/supplier/`
- Optional parts (assistant, forecasting, frontend): **not attempted**

## Setup
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then set DATABASE_URL (PostgreSQL), SUPPLIER_API_URL, SUPPLIER_API_KEY
python manage.py migrate --noinput
python manage.py seed_data
python manage.py seed_data         # safe to run twice
python manage.py runserver 0.0.0.0:8000 --noreload
```

## Run the tests
```bash
python manage.py test
```
The database user needs permission to create a test database. Tests use PostgreSQL, as SQLite is not supported.

Smoke test (server running, mock supplier running):
```bash
python supplier_mock/mock_supplier.py
python smoke_test.py --base-url http://localhost:8000 --track django
```

## Assumptions
- GST is 18% per line, rounded `ROUND_HALF_UP` to the paisa. Order dates use IST.
- Products created by the supplier sync start with stock 0. The sync never changes stock of existing products.
- A DRAFT follows the current catalogue price. The price is locked on confirm.

## Links
- Video walkthrough: **ADD YOUR LINK HERE**
- Design decisions: [DECISIONS.md](DECISIONS.md)
