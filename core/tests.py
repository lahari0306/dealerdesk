import re
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from .models import Dealer, Inventory, Product
from .orders import money


class MoneyTests(SimpleTestCase):
    def test_rounds_half_up(self):
        self.assertEqual(money(Decimal("0.005")), Decimal("0.01"))
        self.assertEqual(money(Decimal("0.045")), Decimal("0.05"))
        self.assertEqual(money(Decimal("1.004")), Decimal("1.00"))


class OrderEngineTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dealer = Dealer.objects.create(dealer_code="T-1", name="Test Dealer")
        self.product = Product.objects.create(sku="P-1", name="Brake Pad", price=Decimal("100.00"))
        Inventory.objects.create(product=self.product, quantity=5)

    def make_order(self, quantity=2, sku="P-1", **extra):
        return self.client.post(
            "/api/orders/",
            {"dealer_id": self.dealer.id, "items": [{"sku": sku, "quantity": quantity}]},
            format="json",
            **extra,
        )

    def stock(self):
        return Inventory.objects.get(product=self.product).quantity

    def test_draft_does_not_touch_stock(self):
        resp = self.make_order(2)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], "DRAFT")
        self.assertEqual(self.stock(), 5)

    def test_order_number_format_and_sequence(self):
        first = self.make_order(1).data["order_number"]
        second = self.make_order(1).data["order_number"]
        self.assertRegex(first, r"^ORD-\d{8}-\d{4}$")
        self.assertEqual(int(second[-4:]), int(first[-4:]) + 1)

    def test_gst_is_rounded_per_line_half_up(self):
        Product.objects.create(sku="P-2", name="Washer", price=Decimal("0.25"))
        Inventory.objects.create(product=Product.objects.get(sku="P-2"), quantity=10)
        resp = self.make_order(1, sku="P-2")
        self.assertEqual(resp.data["tax_total"], "0.05")  # 0.045 rounds UP
        self.assertEqual(resp.data["grand_total"], "0.30")

    def test_confirm_takes_stock(self):
        number = self.make_order(2).data["order_number"]
        resp = self.client.post(f"/api/orders/{number}/confirm/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "CONFIRMED")
        self.assertEqual(self.stock(), 3)

    def test_confirm_with_too_little_stock_changes_nothing(self):
        number = self.make_order(6).data["order_number"]
        resp = self.client.post(f"/api/orders/{number}/confirm/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"]["code"], "INSUFFICIENT_STOCK")
        self.assertEqual(self.stock(), 5)

    def test_cancel_restores_stock_only_once(self):
        number = self.make_order(2).data["order_number"]
        self.client.post(f"/api/orders/{number}/confirm/")
        self.assertEqual(self.client.post(f"/api/orders/{number}/cancel/").status_code, 200)
        self.assertEqual(self.stock(), 5)
        again = self.client.post(f"/api/orders/{number}/cancel/")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["error"]["code"], "INVALID_TRANSITION")
        self.assertEqual(self.stock(), 5)

    def test_invalid_transition_is_rejected(self):
        number = self.make_order(1).data["order_number"]
        resp = self.client.post(f"/api/orders/{number}/dispatch/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"]["code"], "INVALID_TRANSITION")

    def test_price_is_locked_on_confirm(self):
        number = self.make_order(1).data["order_number"]
        self.client.post(f"/api/orders/{number}/confirm/")
        Product.objects.filter(sku="P-1").update(price=Decimal("999.00"))
        resp = self.client.get(f"/api/orders/{number}/")
        self.assertEqual(resp.data["items"][0]["unit_price"], "100.00")

    def test_draft_follows_current_price(self):
        number = self.make_order(1).data["order_number"]
        Product.objects.filter(sku="P-1").update(price=Decimal("120.00"))
        resp = self.client.get(f"/api/orders/{number}/")
        self.assertEqual(resp.data["items"][0]["unit_price"], "120.00")

    def test_same_idempotency_key_returns_same_order(self):
        first = self.make_order(1, HTTP_IDEMPOTENCY_KEY="key-1")
        second = self.make_order(1, HTTP_IDEMPOTENCY_KEY="key-1")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["order_number"], second.data["order_number"])

    def test_idempotency_key_with_different_body_conflicts(self):
        self.make_order(1, HTTP_IDEMPOTENCY_KEY="key-2")
        resp = self.make_order(2, HTTP_IDEMPOTENCY_KEY="key-2")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"]["code"], "IDEMPOTENCY_CONFLICT")