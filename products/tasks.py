"""Tenant task: generate two distinct, realistic Product rows.

A demo business task for the shard+schema-aware Celery layer. Product lives in
TENANT_APPS, so this MUST be enqueued from a TENANT context — the worker then
re-enters that tenant (via tenants.celery) and writes into its schema on its
shard. Enqueued from public it would run on public.public, where there is no
products table.

Guarantees the two requirements:
  * the products' names are DIFFERENT from each other AND from existing rows
    (Product.name is unique → an IntegrityError otherwise);
  * names are realistic ("Premium Ceramic Coffee Mug"), not random characters —
    composed from curated word pools.
"""
import random
from decimal import Decimal

from celery import shared_task
from celery.utils.log import get_task_logger
from django.db import IntegrityError

from .models import Product

logger = get_task_logger(__name__)

_ADJECTIVES = [
    "Premium", "Classic", "Organic", "Deluxe", "Compact", "Eco-Friendly",
    "Smart", "Vintage", "Portable", "Handcrafted", "Lightweight", "Professional",
]
_MATERIALS = [
    "Stainless Steel", "Bamboo", "Ceramic", "Leather", "Cotton", "Glass",
    "Wooden", "Aluminium", "Recycled", "Silicone",
]
_ITEMS = [
    "Water Bottle", "Coffee Mug", "Backpack", "Notebook", "Desk Lamp",
    "Headphones", "Phone Stand", "Travel Mug", "Tote Bag", "Wall Clock",
    "Cutting Board", "Storage Box",
]


def _make_name() -> str:
    """A realistic, human-readable product name (e.g. 'Eco-Friendly Bamboo Mug')."""
    return f"{random.choice(_ADJECTIVES)} {random.choice(_MATERIALS)} {random.choice(_ITEMS)}"


def _pick_distinct_names(count: int, taken: set) -> list:
    """Compose `count` names, distinct from each other and from `taken`.

    The pools give ~1.4k combinations, so collisions are rare; we still guard
    explicitly, and disambiguate with a suffix if the pool ever runs dry.
    """
    names = []
    for _ in range(count * 50):
        if len(names) >= count:
            break
        name = _make_name()
        if name not in taken and name not in names:
            names.append(name)
    suffix = 1
    while len(names) < count:                       # fallback for a near-full table
        candidate = f"{_make_name()} #{suffix}"
        if candidate not in taken and candidate not in names:
            names.append(candidate)
        suffix += 1
    return names


@shared_task
def generate_products(count: int = 2):
    """Create `count` (default 2) realistic, uniquely-named products."""
    existing = set(Product.objects.values_list("name", flat=True))
    names = _pick_distinct_names(count, existing)

    created = []
    for name in names:
        price = (Decimal(random.randrange(199, 9999)) / 100).quantize(Decimal("0.01"))
        try:
            product = Product.objects.create(name=name, price=price)
        except IntegrityError:
            # Lost a race on the unique name — skip rather than fail the task.
            logger.warning("generate_products: name %r taken concurrently, skipping", name)
            continue
        created.append({"id": product.id, "name": product.name, "price": str(product.price)})

    logger.info("generate_products created %d product(s): %s", len(created), created)
    return created
