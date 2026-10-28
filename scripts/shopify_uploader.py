"""
shopify_uploader.py  —  Phase 5
────────────────────────────────────────────────────────────────────
Creates a Shopify product via the Admin REST API and uploads mockup
images (base64-encoded). The Canva template link is stored locally for
post-purchase delivery (order_delivery.py) and is never put in the
public product description.

Run:
    python scripts/shopify_uploader.py <product_name>

Prerequisites (one-time, 💻 HOST):
    python scripts/shopify_setup.py

Settings read from .env:
    SHOPIFY_STORE_DOMAIN  — e.g. your-store.myshopify.com
    SHOPIFY_ACCESS_TOKEN  — Admin API access token (shpat_...)
    SHOPIFY_API_VERSION   — optional, see shopify_common.DEFAULT_API_VERSION
    SHOPIFY_PUBLISH_STATUS — draft (default) or active. Drafts are made
                             live with: ./run.sh approve <product>
    ALLOW_PUBLISH_WITHOUT_TEMPLATE — true to publish even when Phase 3B
                             produced no template link (default false)

Execution flow:
  5.1  Load listing.json from 04_Assets/ReadyToUpload/[ProductName]/
  5.2  Refuse to publish without a template link to deliver
  5.3  Skip creation if a product with this title already exists on Shopify
  5.4  Convert plain-text description to escaped HTML
  5.5  POST /products.json (sent once — never retried after a timeout)
  5.6  Log to SQLite run_log + listings (with template link), mark queue published
"""

from __future__ import annotations

import base64
import html
import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from api_retry import retry_request

from db import insert_listing, log_run, update_queue_status
from shopify_common import api_base, headers, normalize_domain

_ROOT    = Path(__file__).resolve().parent.parent
ENV_PATH = _ROOT / ".env"
load_dotenv(ENV_PATH)

ASSETS_DIR = _ROOT / "04_Assets" / "ReadyToUpload"

DELIVERY_NOTE = (
    "\n\nHow you get it\n"
    "Right after checkout we email you a link to your own editable copy in Canva. "
    "Canva is free to use, and you can edit on any device."
)


def _load_env() -> dict[str, str]:
    return {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}


def _publish_status() -> str:
    status = os.getenv("SHOPIFY_PUBLISH_STATUS", "draft").strip().lower()
    return status if status in ("draft", "active") else "draft"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


# ── Description → HTML ────────────────────────────────────────────────────────

def _to_html(text: str) -> str:
    """
    Convert plain-text product description to basic HTML for Shopify body_html.
    All text is HTML-escaped: descriptions are written by a model, and nothing
    it outputs should reach the storefront as markup.
    """
    lines = text.split("\n")
    html_parts: list[str] = []
    open_list = ""  # "ul", "ol" or "" when no list is open

    def close_list() -> None:
        nonlocal open_list
        if open_list:
            html_parts.append(f"</{open_list}>")
            open_list = ""

    def start_list(tag: str) -> None:
        nonlocal open_list
        if open_list != tag:
            close_list()
            html_parts.append(f"<{tag}>")
            open_list = tag

    for line in lines:
        stripped = line.strip()
        if not stripped:
            close_list()
            continue

        if stripped.startswith(("- ", "• ", "* ")):
            start_list("ul")
            html_parts.append(f"  <li>{html.escape(stripped[2:].strip())}</li>")
        elif stripped[:1].isdigit() and "." in stripped[:3]:
            start_list("ol")
            content = stripped.split(".", 1)[1].strip()
            html_parts.append(f"  <li>{html.escape(content)}</li>")
        elif stripped.startswith("**") and stripped.endswith("**"):
            close_list()
            html_parts.append(f"<strong>{html.escape(stripped.strip('*'))}</strong>")
        elif stripped.startswith("#"):
            close_list()
            heading = stripped.lstrip("#").strip()
            html_parts.append(f"<h3>{html.escape(heading)}</h3>")
        else:
            close_list()
            html_parts.append(f"<p>{html.escape(stripped)}</p>")

    close_list()
    return "\n".join(html_parts)


# ── Image encoding ────────────────────────────────────────────────────────────

def _encode_image(path: Path) -> str:
    """Return base64-encoded JPEG content for Shopify image attachment."""
    return base64.b64encode(path.read_bytes()).decode("utf-8")


# ── Product creation ──────────────────────────────────────────────────────────

def _find_existing_product(domain: str, access_token: str, title: str) -> dict | None:
    """
    Return the Shopify product with this exact title, if one exists.
    Guards against creating a duplicate when an earlier run created the
    product but crashed or timed out before recording it locally.
    """
    resp = retry_request(
        "GET",
        f"{api_base(domain)}/products.json",
        headers=headers(access_token),
        params={"title": title[:255], "fields": "id,handle,title,status", "limit": 5},
        timeout=30,
        _label="Shopify find product",
    )
    if not resp.ok:
        raise RuntimeError(
            f"Could not check for an existing product ({resp.status_code}): {resp.text[:300]}"
        )
    for product in resp.json().get("products", []):
        if product.get("title") == title[:255]:
            return product
    return None


def _create_product(
    domain: str,
    access_token: str,
    payload: dict,
    image_paths: list[Path],
) -> dict:
    """
    POST a new Shopify product. Returns the created product dict.
    Images are sent as base64 attachments in the same request.
    The request is sent once: retrying after a timeout could create a duplicate.
    """
    tags_raw = payload.get("tags", [])
    tags_str = ", ".join(str(t) for t in tags_raw)

    description_text = payload.get("description", "") + DELIVERY_NOTE
    body_html = _to_html(description_text)

    images = [
        {"attachment": _encode_image(p), "filename": p.name, "position": i + 1}
        for i, p in enumerate(image_paths[:10])
    ]

    product_body = {
        "product": {
            "title":        payload["title"][:255],
            "body_html":    body_html,
            "vendor":       os.getenv("SHOPIFY_VENDOR", os.getenv("BRAND_NAME", "Your Brand")),
            "product_type": payload.get("category", "Digital Template"),
            "tags":         tags_str,
            "status":       _publish_status(),
            "variants": [{
                "price":                str(float(payload["price"])),
                "requires_shipping":    False,
                "inventory_management": None,
                "inventory_policy":     "continue",
                "fulfillment_service":  "manual",
            }],
            "images": images,
        }
    }

    resp = retry_request(
        "POST",
        f"{api_base(domain)}/products.json",
        headers=headers(access_token),
        json=product_body,
        timeout=180,
        _label="Shopify create product",
    )

    if not resp.ok:
        raise RuntimeError(
            f"Product creation failed ({resp.status_code}): {resp.text[:400]}"
        )

    return resp.json()["product"]


# ── Main ──────────────────────────────────────────────────────────────────────

def upload_listing(product_name: str) -> str | None:
    """
    Full Phase 5 upload via Shopify Admin API.
    Returns the product URL on success, None on failure.
    """
    env = _load_env()

    domain       = normalize_domain(env.get("SHOPIFY_STORE_DOMAIN", ""))
    access_token = env.get("SHOPIFY_ACCESS_TOKEN", "")

    if not domain or not access_token:
        raise RuntimeError(
            "SHOPIFY_STORE_DOMAIN and SHOPIFY_ACCESS_TOKEN are required. "
            "Run: python scripts/shopify_setup.py"
        )

    payload_file = ASSETS_DIR / product_name / "listing.json"
    if not payload_file.exists():
        raise FileNotFoundError(
            "listing.json not found — run listing_builder.py first"
        )

    payload = json.loads(payload_file.read_text(encoding="utf-8"))

    # Collect mockup images (ReadyToUpload dir or Mockups dir)
    mockups_dir  = _ROOT / "02_Products" / product_name / "Mockups"
    image_paths  = sorted(mockups_dir.glob("*.jpg")) if mockups_dir.exists() else []
    if not image_paths:
        image_paths = sorted((ASSETS_DIR / product_name).glob("*.jpg"))
    if not image_paths:
        raise FileNotFoundError(
            f"No JPEG mockups found for {product_name}"
        )

    print(f"  [5.1] Loaded listing.json for: {product_name}")
    print(f"  [5.1] Images: {len(image_paths)} mockups")

    template_link = payload.get("template_link", "")
    if not template_link and not _env_flag("ALLOW_PUBLISH_WITHOUT_TEMPLATE"):
        raise RuntimeError(
            f"No template link for {product_name} — buyers would have nothing to download. "
            "Re-run Phase 3B (./run.sh phase3b) before publishing."
        )

    existing = _find_existing_product(domain, access_token, payload["title"])
    if existing:
        product = existing
        print(f"  [5.3] Product already exists on Shopify (ID {product['id']}) — not creating again")
    else:
        product = _create_product(domain, access_token, payload, image_paths)
        print(f"  [5.5] Product created: ID {product['id']}")

    product_id     = product["id"]
    product_status = product.get("status", _publish_status())
    product_handle = product.get("handle", "")
    product_url    = f"https://{domain}/products/{product_handle}"

    if product_status == "active":
        print(f"  [5.6] Listing live: {product_url}")
    else:
        print(f"  [5.6] Saved as {product_status} — review it in Shopify admin, then run: "
              f"./run.sh approve \"{product_name}\"")

    log_run(
        product_name,
        "shopify_uploader",
        "success",
        f"Product {product_status} via Admin API — ID {product_id}",
        shopify_url=product_url,
    )
    insert_listing(
        product_name,
        payload["title"],
        product_url,
        shopify_product_id=product_id,
        template_link=template_link,
        status=product_status,
    )
    update_queue_status(payload["queue_id"], "published")

    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from file_organizer import archive
        archive(product_name)
    except Exception as exc:
        print(f"  [5.6] Archive warning (non-fatal): {exc}")

    return product_url


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/shopify_uploader.py <product_name>")
        sys.exit(1)

    try:
        result = upload_listing(sys.argv[1])
        sys.exit(0 if result else 1)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[error] {e}")
        sys.exit(1)
