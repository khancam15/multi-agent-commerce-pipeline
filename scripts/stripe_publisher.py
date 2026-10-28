"""
stripe_publisher.py  —  Phase 5
────────────────────────────────────────────────────────────────────
Creates the Stripe product and price for a staged listing, saves
web-sized images for the storefront, and records the listing locally.
The Canva template link is stored for post-purchase delivery
(order_delivery.py) and never shown publicly.

New products are drafts: they have no payment link and do not appear on
the storefront until approved (./run.sh approve "<product>"), unless
STORE_PUBLISH_STATUS=active.

Run:
    python scripts/stripe_publisher.py <product_name>

Settings read from .env:
    STRIPE_SECRET_KEY      — see stripe_common.py
    STOREFRONT_BASE_URL    — public URL of the storefront, e.g.
                             https://you.github.io/store (used for image links)
    STORE_PUBLISH_STATUS   — draft (default) or active
    ALLOW_PUBLISH_WITHOUT_TEMPLATE — true to publish even when Phase 3B
                             produced no template link (default false)

Execution flow:
  5.1  Load listing.json from 04_Assets/ReadyToUpload/[ProductName]/
  5.2  Refuse to publish without a template link to deliver
  5.3  Save 1200px images to outputs/storefront/assets/<slug>/
  5.4  Reuse the Stripe product for this queue item if one exists,
       otherwise create it with its price (idempotent)
  5.5  Active mode only: create the payment link
  5.6  Record the listing in SQLite and mark the queue row published
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from db import get_listing, insert_listing, log_run, update_queue_status
from storefront import ASSETS_DIR as STOREFRONT_ASSETS
from storefront import plain_text, product_page_url, save_web_images, unique_slug
from stripe_common import StripeError, currency, request, to_minor_units

_ROOT    = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

ASSETS_DIR = _ROOT / "04_Assets" / "ReadyToUpload"
MAX_STRIPE_IMAGES = 8


def _publish_status() -> str:
    status = os.getenv("STORE_PUBLISH_STATUS", "draft").strip().lower()
    return status if status in ("draft", "active") else "draft"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _find_product(queue_id: int) -> dict | None:
    """Stripe product already created for this queue item, if any."""
    result = request(
        "GET",
        "/products/search",
        params={"query": f"metadata['queue_id']:'{queue_id}'", "limit": 1},
        label="Stripe find product",
    )
    found = result.get("data", [])
    return found[0] if found else None


def _create_product(payload: dict, slug: str, image_urls: list[str]) -> dict:
    """Create the product and its default price. Safe to repeat for the same queue item."""
    data = {
        "name": payload["title"][:250],
        "description": plain_text(payload.get("description", ""))[:5000] or None,
        "images": image_urls[:MAX_STRIPE_IMAGES] or None,
        "url": product_page_url(slug) or None,
        "metadata": {
            "queue_id": str(payload["queue_id"]),
            "product_name": payload["product_name"][:400],
            "slug": slug,
            "source": "multi-agent-commerce-pipeline",
        },
        "default_price_data": {
            "currency": currency(),
            "unit_amount": to_minor_units(payload["price"]),
        },
    }
    return request(
        "POST",
        "/products",
        data=data,
        idempotency_key=f"product-q{payload['queue_id']}-{slug}",
        label="Stripe create product",
    )


def create_payment_link(product_name: str, price_id: str, ref: object) -> dict:
    """Payment link for one unit of the product, with a delivery message after checkout."""
    return request(
        "POST",
        "/payment_links",
        data={
            "line_items": [{"price": price_id, "quantity": 1}],
            "after_completion": {
                "type": "hosted_confirmation",
                "hosted_confirmation": {
                    "custom_message": (
                        "Thank you! We're emailing you a link to your own editable "
                        "copy in Canva. Check your inbox (and spam folder) in a few minutes."
                    ),
                },
            },
            "metadata": {"product_name": product_name[:400], "ref": str(ref)},
        },
        idempotency_key=f"payment-link-{ref}-{price_id}",
        label="Stripe create payment link",
    )


def publish_listing(product_name: str) -> str | None:
    """
    Full Phase 5. Returns the storefront page URL (or the payment link when no
    storefront URL is configured) on success.
    """
    payload_file = ASSETS_DIR / product_name / "listing.json"
    if not payload_file.exists():
        raise FileNotFoundError("listing.json not found — run listing_builder.py first")
    payload = json.loads(payload_file.read_text(encoding="utf-8"))

    mockups_dir = _ROOT / "02_Products" / product_name / "Mockups"
    image_paths = sorted(mockups_dir.glob("*.jpg")) if mockups_dir.exists() else []
    if not image_paths:
        image_paths = sorted((ASSETS_DIR / product_name).glob("*.jpg"))
    if not image_paths:
        raise FileNotFoundError(f"No JPEG mockups found for {product_name}")

    print(f"  [5.1] Loaded listing.json for: {product_name} ({len(image_paths)} mockups)")

    template_link = payload.get("template_link", "")
    if not template_link and not _env_flag("ALLOW_PUBLISH_WITHOUT_TEMPLATE"):
        raise RuntimeError(
            f"No template link for {product_name} — buyers would have nothing to download. "
            "Re-run Phase 3B (./run.sh phase3b) before publishing."
        )

    existing_listing = get_listing(product_name)
    slug = (existing_listing["slug"] if existing_listing and existing_listing["slug"]
            else unique_slug(payload["title"]))
    images = save_web_images(slug, image_paths)
    base = os.getenv("STOREFRONT_BASE_URL", "").rstrip("/")
    image_urls = [f"{base}/{p}" for p in images] if base else []
    print(f"  [5.3] Saved {len(images)} storefront image(s) to {STOREFRONT_ASSETS / slug}")

    product = _find_product(payload["queue_id"])
    if product:
        print(f"  [5.4] Stripe product already exists ({product['id']}) — not creating again")
    else:
        product = _create_product(payload, slug, image_urls)
        print(f"  [5.4] Stripe product created: {product['id']}")
    price_id = product.get("default_price") or ""
    if isinstance(price_id, dict):
        price_id = price_id.get("id", "")

    status = _publish_status()
    link_id = link_url = None
    if status == "active":
        link = create_payment_link(product_name, price_id, f"q{payload['queue_id']}")
        link_id, link_url = link["id"], link["url"]
        print(f"  [5.5] Payment link: {link_url}")
    else:
        print(f"  [5.5] Saved as draft — review it, then run: ./run.sh approve \"{product_name}\"")

    page_url = product_page_url(slug)
    insert_listing(
        product_name,
        payload["title"],
        template_link=template_link,
        status=status,
        stripe_product_id=product["id"],
        stripe_price_id=price_id,
        payment_link_id=link_id,
        payment_link_url=link_url,
        page_url=page_url or None,
        slug=slug,
        price=float(payload["price"]),
        currency=currency(),
        description=payload.get("description", ""),
        category=payload.get("category", ""),
        tags=json.dumps(payload.get("tags", [])),
        images=json.dumps(images),
    )
    log_run(product_name, "stripe_publisher", "success",
            f"Stripe product {product['id']} saved as {status}", shopify_url=page_url or None)
    update_queue_status(payload["queue_id"], "published")

    try:
        from file_organizer import archive
        archive(product_name)
    except Exception as exc:
        print(f"  [5.6] Archive warning (non-fatal): {exc}")

    return page_url or link_url or product["id"]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/stripe_publisher.py <product_name>")
        sys.exit(1)
    try:
        result = publish_listing(sys.argv[1])
        print(f"  [5] Done: {result}")
        sys.exit(0 if result else 1)
    except (FileNotFoundError, RuntimeError, StripeError) as e:
        print(f"[error] {e}")
        sys.exit(1)
