"""
Засіяти товари у каталог тенанта.

Запускається через django-tenants `tenant_command` обгортку — вона сама
переключить search_path на потрібну схему і провалідує, що тенант існує:

    python manage.py tenant_command seed_products
    # → tenant_command інтерактивно запитає, в який тенант сіяти

    python manage.py tenant_command seed_products --schema=alpha
    # одразу в alpha, без інтерактиву

    python manage.py tenant_command seed_products --schema=alpha --count=20 --reset
"""

import random
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from products.models import Product


# 15 готових позицій; за замовчуванням команда обирає 10 випадково.
SAMPLE_PRODUCTS = [
    ("Болт M12 × 80",          Decimal("4.50")),
    ("Гайка M12",              Decimal("2.10")),
    ("Шайба плоска 12 мм",     Decimal("0.85")),
    ("Шуруп 6 × 60",           Decimal("3.20")),
    ("Дюбель 8 × 40",          Decimal("1.10")),
    ("Анкер 10 × 100",         Decimal("12.30")),
    ("Саморіз гіпсокартонний", Decimal("0.45")),
    ("Цвях 100 мм",            Decimal("0.30")),
    ("Шплінт 4 × 32",          Decimal("0.70")),
    ("Хомут затяжний 5 × 150", Decimal("1.85")),
    ("Кабельна стяжка 200 мм", Decimal("0.20")),
    ("Ізолента ПВХ синя",      Decimal("18.50")),
    ("Скоч пакувальний 50 мм", Decimal("32.00")),
    ("Маркер перманентний",    Decimal("28.40")),
    ("Викрутка хрестова PH2",  Decimal("89.00")),
]


class Command(BaseCommand):
    help = (
        "Засіяти приклади товарів у каталог поточного тенанта. "
        "Запускати ТІЛЬКИ через `manage.py tenant_command seed_products`."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=10,
            help="Скільки товарів додати. Дефолт — 10.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Видалити наявні товари перед додаванням.",
        )

    def handle(self, *args, **opts):
        # Захист: якщо команду запустили напряму (без tenant_command),
        # search_path лишиться на public — а в public нема `products_product`.
        # Видамо чітку помилку замість підступного `ProgrammingError`.
        if connection.schema_name == "public":
            raise CommandError(
                "Цю команду треба запускати через "
                "`python manage.py tenant_command seed_products` "
                "(інакше товари впадуть у public-схему, де немає products_product)."
            )

        count = opts["count"]
        if count < 1:
            raise CommandError("--count має бути >= 1.")

        if opts["reset"]:
            deleted, _ = Product.objects.all().delete()
            if deleted:
                self.stdout.write(
                    self.style.WARNING(
                        f"[{connection.schema_name}] видалено {deleted} наявний товар(и) до сівби."
                    )
                )

        chosen = self._pick(count)

        created = 0
        for name, price in chosen:
            _, was_created = Product.objects.get_or_create(
                name=name,
                defaults={"price": price},
            )
            if was_created:
                created += 1

        already = count - created
        msg = f"[{connection.schema_name}] додано {created} новий товар(ів)"
        if already:
            msg += f", {already} вже існували"
        msg += "."
        self.stdout.write(self.style.SUCCESS(msg))

    @staticmethod
    def _pick(count: int) -> list[tuple[str, Decimal]]:
        """Обирає `count` позицій із SAMPLE_PRODUCTS.

        Якщо `count` більший за розмір шаблону — добиваємо дублями з
        суфіксом «(#N)», щоб не порушити Product.name UNIQUE.
        """
        sample = SAMPLE_PRODUCTS
        if count <= len(sample):
            return random.sample(sample, count)

        result = list(sample)
        extras_needed = count - len(sample)
        i = 0
        while extras_needed > 0:
            base_name, base_price = sample[i % len(sample)]
            suffix = (i // len(sample)) + 2
            result.append((f"{base_name} (#{suffix})", base_price))
            i += 1
            extras_needed -= 1
        return result
