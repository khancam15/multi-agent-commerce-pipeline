"""
sales_tracker.py — Daily sales & revenue sync
───────────────────────────────────────────────
Pulls order data from the Shopify Admin REST API, maps each sale to a
product name via the listings table, and stores everything in the
SQLite `sales` table.

Run:
    python scripts/sales_tracker.py          — sync new orders only
    python scripts/sales_tracker.py --full   — re-fetch all orders (slow)

Called by loop.sh once per day alongside the email digest.

Shopify API used:
    GET /admin/api/<SHOPIFY_API_VERSION>/orders.json
    Fields: id, line_items (product_id, title, price, quantity),
            total_price, total_discounts, created_at, financial_status

Requires in .env:
    SHOPIFY_STORE_DOMAIN   — e.g. your-store.myshopify.com
    SHOPIFY_ACCESS_TOKEN   — Admin API access token (shpat_...)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
ENV_PATH = _ROOT / ".env"

sys.path.insert(0, str(Path(__file__).parent))
from db import (
    get_conn,
    get_latest_sale_date,
    get_sales_summary,
    init_db,
    log_run,
    upsert_sale,
)
from api_retry import rget
from shopify_common import api_version

API_VERSION = api_version()
PAGE_SIZE   = 250  # Shopify max per page


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_creds() -> dict[str, str]:
    return {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}


def _normalize_domain(domain: str) -> str:
    return domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def _api_base(domain: str) -> str:
    domain = _normalize_domain(domain)
    return f"https://{domain}/admin/api/{API_VERSION}"


def _hdrs(access_token: str) -> dict[str, str]:
    return {"X-Shopify-Access-Token": access_token}


# ── Product ID → product name cache ───────────────────────────────────────────

def _build_listing_map() -> dict[str, str]:
    """
    Build a map of Shopify product IDs and handles to local product names.
    Orders provide product_id; handles are kept as a fallback for older rows.
    """
    mapping: dict[str, str] = {}
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT product_name, shopify_url, shopify_product_id
               FROM listings
               WHERE shopify_url IS NOT NULL OR shopify_product_id IS NOT NULL"""
        ).fetchall()
    for row in rows:
        product_id = str(row["shopify_product_id"] or "").strip()
        if product_id:
            mapping[product_id] = row["product_name"]

        url = row["shopify_url"] or ""
        parts = url.rstrip("/").split("/")
        for i, part in enumerate(parts):
            if part == "products" and i + 1 < len(parts):
                mapping[parts[i + 1]] = row["product_name"]
                break
    return mapping


# ── Order parsing ─────────────────────────────────────────────────────────────

def _parse_order(order: dict[str, Any], listing_map: dict[str, str]) -> list[dict[str, Any]]:
    """
    Convert a raw Shopify order into a list of normalised sale dicts (one per line item).
    Returns empty list if order is missing essential fields or not paid.
    """
    financial_status = order.get("financial_status", "")
    if financial_status not in ("paid", "partially_paid"):
        return []

    order_id = str(order.get("id", ""))
    if not order_id:
        return []

    try:
        sale_date = datetime.fromisoformat(
            order["created_at"].replace("Z", "+00:00")
        ).isoformat()
    except (KeyError, ValueError, AttributeError):
        sale_date = datetime.now(tz=timezone.utc).isoformat()

    sales: list[dict[str, Any]] = []
    for item in order.get("line_items", []):
        item_id    = str(item.get("id", ""))
        product_id = str(item.get("product_id", ""))
        title      = (item.get("title") or "")[:200]
        quantity   = int(item.get("quantity", 1))
        gross_unit = float(item.get("price", "0"))
        total_gross = round(gross_unit * quantity, 2)

        # Shopify fees: ~2.9% + $0.30 payment processing; no direct fee field in orders
        # Approximate net as gross minus payment processing estimate
        payment_fee = round(total_gross * 0.029 + 0.30, 2)
        net_amount  = round(total_gross - payment_fee, 2)

        transaction_id = f"shopify_{order_id}_{item_id}"
        product_name   = listing_map.get(product_id, "")

        currency = order.get("currency", "USD")

        sales.append({
            "transaction_id": transaction_id,
            "listing_id":     product_id,
            "product_name":   product_name,
            "title":          title,
            "gross_amount":   total_gross,
            "amount":         net_amount,
            "quantity":       quantity,
            "currency":       currency,
            "sale_date":      sale_date,
        })

    return sales


# ── Fetch orders ──────────────────────────────────────────────────────────────

def _fetch_orders(
    domain: str,
    access_token: str,
    created_at_min: str | None = None,
    page_info: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Fetch one page of orders from the Shopify API.
    Returns (orders, next_page_info) — next_page_info is None on the last page.
    Shopify uses cursor-based pagination via Link header.
    """
    params: dict[str, Any] = {
        "status":  "any",
        "limit":   PAGE_SIZE,
        "fields":  "id,line_items,total_price,currency,financial_status,created_at",
    }
    if created_at_min:
        params["created_at_min"] = created_at_min
    if page_info:
        params = {"page_info": page_info, "limit": PAGE_SIZE}

    resp = rget(
        f"{_api_base(domain)}/orders.json",
        headers=_hdrs(access_token),
        params=params,
        timeout=30,
        _label="Shopify orders",
    )
    resp.raise_for_status()

    orders = resp.json().get("orders", [])

    # Parse cursor for next page from Link header
    next_page_info: str | None = None
    link_header = resp.headers.get("Link", "")
    if 'rel="next"' in link_header:
        for part in link_header.split(","):
            if 'rel="next"' in part:
                # Extract page_info from URL: <url?page_info=xyz>; rel="next"
                url_part = part.split(";")[0].strip().strip("<>")
                for param in url_part.split("?")[-1].split("&"):
                    if param.startswith("page_info="):
                        next_page_info = param.split("=", 1)[1]
                        break

    return orders, next_page_info


# ── Main sync ─────────────────────────────────────────────────────────────────

def sync_sales(full: bool = False) -> dict[str, Any]:
    """
    Sync Shopify orders to the local sales table.

    Args:
        full: If True, re-fetch all orders (ignores last sync date).

    Returns:
        Summary dict with new_sales, total_revenue_7d, etc.
    """
    init_db()

    creds = _load_creds()
    domain       = _normalize_domain(creds.get("SHOPIFY_STORE_DOMAIN", ""))
    access_token = creds.get("SHOPIFY_ACCESS_TOKEN", "")

    if not domain or not access_token:
        raise RuntimeError(
            "SHOPIFY_STORE_DOMAIN and SHOPIFY_ACCESS_TOKEN not set in .env — "
            "run: python scripts/shopify_setup.py"
        )

    listing_map = _build_listing_map()
    print(f"  [sales] Listing map: {len(listing_map)} products")

    created_at_min: str | None = None
    if not full:
        last_date = get_latest_sale_date()
        if last_date:
            created_at_min = last_date
            print(f"  [sales] Incremental sync from: {last_date[:10]}")
    if full or created_at_min is None:
        print("  [sales] Full sync — fetching all orders")

    new_count  = 0
    page_info: str | None = None

    while True:
        orders, next_page_info = _fetch_orders(
            domain, access_token,
            created_at_min=created_at_min if not page_info else None,
            page_info=page_info,
        )
        if not orders:
            break

        for order in orders:
            for sale in _parse_order(order, listing_map):
                inserted = upsert_sale(**sale)
                if inserted:
                    new_count += 1

        if not next_page_info:
            break
        page_info = next_page_info

    summary = get_sales_summary(days=7)
    summary["new_transactions"] = new_count

    print(f"  [sales] ✓ Synced {new_count} new order(s)")
    print(f"  [sales] Last 7 days: {summary['order_count']} orders | "
          f"${summary['total_revenue']:.2f} net | "
          f"${summary['gross_revenue']:.2f} gross")
    if summary["best_product"]:
        print(f"  [sales] Best product: {summary['best_product']} "
              f"(${summary['best_product_revenue']:.2f})")
    print(f"  [sales] All-time revenue: ${summary['all_time_revenue']:.2f}")

    log_run(
        "pipeline",
        "sales_tracker",
        "success",
        f"new={new_count}, 7d_revenue=${summary['total_revenue']:.2f}, "
        f"7d_orders={summary['order_count']}",
    )
    return summary


if __name__ == "__main__":
    full_sync = "--full" in sys.argv
    try:
        result = sync_sales(full=full_sync)
        sys.exit(0)
    except RuntimeError as e:
        print(f"[error] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[error] Unexpected: {e}")
        sys.exit(1)
