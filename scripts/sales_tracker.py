"""
sales_tracker.py — Daily sales & revenue sync
───────────────────────────────────────────────
Pulls paid Stripe Checkout Sessions (every Payment Link purchase is one),
maps each line item to a product name via the listings table, and stores
everything in the SQLite `sales` table.

Net revenue uses the actual Stripe fee from each payment's balance
transaction, split across line items by amount. When the fee is not
available yet, it falls back to an estimate of 2.9% + 30¢.

Run:
    python scripts/sales_tracker.py          — sync new sales only
    python scripts/sales_tracker.py --full   — re-fetch all sales (slow)

Called by loop.sh once per day alongside the email digest.

Requires in .env:
    STRIPE_SECRET_KEY — see stripe_common.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from db import (
    get_conn,
    get_latest_sale_date,
    get_sales_summary,
    init_db,
    log_run,
    upsert_sale,
)
from stripe_common import StripeError, line_item_product_id, paid_checkout_sessions

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

FEE_EXPAND = "data.payment_intent.latest_charge.balance_transaction"


def _build_listing_map() -> dict[str, str]:
    """Map Stripe product IDs to local product names."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT product_name, stripe_product_id FROM listings "
            "WHERE COALESCE(stripe_product_id, '') != ''"
        ).fetchall()
    return {row["stripe_product_id"]: row["product_name"] for row in rows}


def _session_fee(session: dict[str, Any]) -> float | None:
    """Actual Stripe fee for the session's payment in major units, if known."""
    intent = session.get("payment_intent")
    if not isinstance(intent, dict):
        return None
    charge = intent.get("latest_charge")
    if not isinstance(charge, dict):
        return None
    txn = charge.get("balance_transaction")
    if not isinstance(txn, dict) or txn.get("fee") is None:
        return None
    return txn["fee"] / 100


def parse_session(session: dict[str, Any], listing_map: dict[str, str]) -> list[dict[str, Any]]:
    """Convert a paid Checkout Session into one normalised sale dict per line item."""
    session_id = str(session.get("id", ""))
    if not session_id:
        return []
    created = session.get("created")
    sale_date = (
        datetime.fromtimestamp(created, tz=UTC).isoformat() if created
        else datetime.now(UTC).isoformat()
    )
    lines = (session.get("line_items") or {}).get("data", [])
    session_total = sum(int(li.get("amount_total") or 0) for li in lines) / 100
    actual_fee = _session_fee(session)

    sales: list[dict[str, Any]] = []
    for line in lines:
        gross = int(line.get("amount_total") or 0) / 100
        if actual_fee is not None and session_total > 0:
            fee = actual_fee * gross / session_total
        else:
            fee = gross * 0.029 + 0.30 / max(len(lines), 1)
        product_id = line_item_product_id(line)
        sales.append({
            "transaction_id": f"stripe_{session_id}_{line.get('id', product_id)}",
            "listing_id":     product_id,
            "product_name":   listing_map.get(product_id, ""),
            "title":          (line.get("description") or "")[:200],
            "gross_amount":   round(gross, 2),
            "amount":         round(gross - fee, 2),
            "quantity":       int(line.get("quantity") or 1),
            "currency":       (session.get("currency") or "usd").upper(),
            "sale_date":      sale_date,
        })
    return sales


def sync_sales(full: bool = False) -> dict[str, Any]:
    """Sync paid Stripe checkouts to the local sales table."""
    init_db()
    listing_map = _build_listing_map()
    print(f"  [sales] Listing map: {len(listing_map)} products")

    since = datetime(2020, 1, 1, tzinfo=UTC)
    last_date = None if full else get_latest_sale_date()
    if last_date:
        # Overlap one day: upsert_sale skips rows already stored
        since = datetime.fromisoformat(last_date).astimezone(UTC) - timedelta(days=1)
        print(f"  [sales] Incremental sync from: {since.date()}")
    else:
        print("  [sales] Full sync — fetching all paid checkouts")

    new_count = 0
    for session in paid_checkout_sessions(since, expand=[FEE_EXPAND]):
        for sale in parse_session(session, listing_map):
            if upsert_sale(**sale):
                new_count += 1

    summary = get_sales_summary(days=7)
    summary["new_transactions"] = new_count

    print(f"  [sales] ✓ Synced {new_count} new sale(s)")
    print(f"  [sales] Last 7 days: {summary['order_count']} sales | "
          f"${summary['total_revenue']:.2f} net | ${summary['gross_revenue']:.2f} gross")
    if summary["best_product"]:
        print(f"  [sales] Best product: {summary['best_product']} "
              f"(${summary['best_product_revenue']:.2f})")
    print(f"  [sales] All-time revenue: ${summary['all_time_revenue']:.2f}")

    log_run(
        "pipeline", "sales_tracker", "success",
        f"new={new_count}, 7d_revenue=${summary['total_revenue']:.2f}, "
        f"7d_orders={summary['order_count']}",
    )
    return summary


if __name__ == "__main__":
    try:
        sync_sales(full="--full" in sys.argv)
        sys.exit(0)
    except StripeError as e:
        print(f"[error] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[error] Unexpected: {e}")
        sys.exit(1)
