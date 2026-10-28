"""
approve_listing.py — Make a reviewed draft product live
────────────────────────────────────────────────────────
Phase 5 saves products as drafts by default (STORE_PUBLISH_STATUS=draft)
so a person can check the AI-written copy and images first. Drafts have
no payment link and are not on the storefront.

Preview a draft:
    python scripts/approve_listing.py --preview "Product Name"
        — builds site_preview/ with the draft included, to open in a browser
Approve it:
    python scripts/approve_listing.py "Product Name"
        — creates the Stripe payment link, marks the listing active,
          and rebuilds and deploys the storefront
List drafts:
    python scripts/approve_listing.py --list

or ./run.sh approve ["Product Name" | --list | --preview "Product Name"]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import storefront
from db import get_conn, get_listing, log_run, update_listing
from stripe_common import StripeError, request
from stripe_publisher import create_payment_link

_ROOT = Path(__file__).resolve().parent.parent


def drafts() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT product_name FROM listings WHERE status = 'draft' ORDER BY published_at"
        ).fetchall()
    return [r["product_name"] for r in rows]


def approve(product_name: str) -> str:
    row = get_listing(product_name)
    if row is None or not row["stripe_product_id"]:
        raise RuntimeError(f"No published listing found for {product_name}")
    if not row["template_link"]:
        raise RuntimeError(
            f"{product_name} has no template link, so buyers could not be sent the product. "
            "Re-run Phase 3B first."
        )

    if row["payment_link_id"]:
        link = request("POST", f"/payment_links/{row['payment_link_id']}",
                       data={"active": True}, label="Stripe activate payment link")
    else:
        link = create_payment_link(product_name, row["stripe_price_id"], row["stripe_product_id"])

    update_listing(product_name, status="active",
                   payment_link_id=link["id"], payment_link_url=link["url"])
    log_run(product_name, "approve_listing", "success", f"Payment link {link['id']} active")

    storefront.build()
    try:
        storefront.deploy()
    except Exception as exc:  # the product is live in Stripe either way
        print(f"  [approve] Warning: storefront deploy failed ({exc}) — run ./run.sh store-deploy")
    return row["page_url"] or link["url"]


def preview(product_name: str) -> Path:
    """Build a local copy of the site that includes this draft, for review."""
    row = get_listing(product_name)
    if row is None:
        raise RuntimeError(f"No listing found for {product_name}")
    out = _ROOT / "site_preview"
    original = storefront.storefront_listings

    def with_draft():
        live = list(original())
        if row["status"] != "active":
            fake = dict(row)
            fake["payment_link_url"] = fake["payment_link_url"] or "#draft-not-for-sale"
            live.insert(0, fake)
        return live

    storefront.storefront_listings = with_draft
    try:
        storefront.build(site_dir=out)
    finally:
        storefront.storefront_listings = original
    return out / "p" / row["slug"] / "index.html"


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if not args or args[0] == "--list":
            waiting = drafts()
            print("Drafts waiting for review:" if waiting else "No drafts waiting for review.")
            for name in waiting:
                print(f"  - {name}")
        elif args[0] == "--preview" and len(args) > 1:
            print(f"  [approve] Preview written: {preview(args[1])}")
        else:
            print(f"  [approve] Live: {approve(args[0])}")
    except (RuntimeError, StripeError) as e:
        print(f"[error] {e}")
        sys.exit(1)
