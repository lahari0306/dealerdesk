import csv
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Dealer, Inventory, Product


class Command(BaseCommand):
    help = "Load catalogue.csv and dealers.csv. Safe to run more than once."

    @transaction.atomic
    def handle(self, *args, **options):
        data_dir = settings.DATA_DIR

        with open(data_dir / "catalogue.csv", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                product, _ = Product.objects.update_or_create(
                    sku=row["sku"].strip(),
                    defaults={
                        "name": row["name"],
                        "category": row["category"],
                        "brand": row["brand"],
                        "vehicle_fitment": row["vehicle_fitment"],
                        "description": row["description"],
                        "price": Decimal(row["price"]),
                    },
                )
                Inventory.objects.get_or_create(
                    product=product,
                    defaults={"quantity": int(row["stock"])},
                )

        with open(data_dir / "dealers.csv", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                Dealer.objects.update_or_create(
                    dealer_code=row["dealer_code"].strip(),
                    defaults={
                        "name": row["name"],
                        "city": row["city"],
                        "phone": row["phone"],
                        "email": row["email"],
                        "gstin": row["gstin"],
                        "credit_limit": Decimal(row["credit_limit"]),
                    },
                )

        self.stdout.write(self.style.SUCCESS("Seed complete."))