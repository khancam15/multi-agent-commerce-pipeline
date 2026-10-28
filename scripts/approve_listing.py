"""
approve_listing.py — Make a reviewed draft product live
────────────────────────────────────────────────────────
Phase 5 creates products as drafts by default (SHOPIFY_PUBLISH_STATUS=draft)
so a person can check the AI-written copy and images first. After reviewing
a draft in Shopify admin, run:

    python scripts/approve_listing.py "Product Name"
    python scripts/approve_listing.py --list        — show drafts waiting for review

or ./run.sh approve "Product Name"
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).parent))
from api_retry import retry_request
from db import get_conn, log_run, set_listing_status
from shopify_common import api_base, headers, normalize_domain

_ROOT    = Path(__file__).resolve().parent.parent
ENV_PATH = _ROOT / ".env"


def drafts() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT product_name FROM listings WHERE status = 'draft' ORDER BY published_at"
        ).fetchall()
    return [r["product_name"] for r in rows]


def approve(product_name: str) -> str:
    env    = {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}
    domain = normalize_domain(env.get("SHOPIFY_STORE_DOMAIN", ""))
    token  = env.get("SHOPIFY_ACCESS_TOKEN", "")
    if not domain or not token:
        raise RuntimeError("SHOPIFY_STORE_DOMAIN and SHOPIFY_ACCESS_TOKEN are required")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT shopify_product_id, shopify_url, template_link FROM listings "
            "WHERE product_name = ? ORDER BY published_at DESC LIMIT 1",
            (product_name,),
        ).fetchone()
    if row is None or not row["shopify_product_id"]:
        raise RuntimeError(f"No published listing found for {product_name}")
    if not row["template_link"]:
        raise RuntimeError(
            f"{product_name} has no template link, so buyers could not be sent the product. "
            "Re-run Phase 3B first."
        )

    product_id = row["shopify_product_id"]
    resp = retry_request(
        "PUT",
        f"{api_base(domain)}/products/{product_id}.json",
        headers=headers(token),
        json={"product": {"id": int(product_id), "status": "active"}},
        timeout=30,
        _label="Shopify approve product",
    )
    if not resp.ok:
        raise RuntimeError(f"Approve failed ({resp.status_code}): {resp.text[:300]}")

    set_listing_status(product_name, "active")
    log_run(product_name, "approve_listing", "success", f"Product {product_id} set active")
    return row["shopify_url"] or ""


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        waiting = drafts()
        print("Drafts waiting for review:" if waiting else "No drafts waiting for review.")
        for name in waiting:
            print(f"  - {name}")
        sys.exit(0)
    try:
        url = approve(args[0])
        print(f"  [approve] Live: {url}")
    except RuntimeError as e:
        print(f"[error] {e}")
        sys.exit(1)
