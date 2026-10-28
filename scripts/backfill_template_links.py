"""
backfill_template_links.py — Fix products published before private delivery
────────────────────────────────────────────────────────────────────────────
Products published by older versions of the pipeline have the Canva template
link in their public description, and no template link stored locally, so
order_delivery.py cannot email them to buyers.

For each published listing this script:
  1. Stores the template link from outputs/<Product>_product.json in SQLite
  2. Removes the template block and any canva.com links from the live
     product description on Shopify (only with --apply)

Run:
    python scripts/backfill_template_links.py            — dry run, changes nothing
    python scripts/backfill_template_links.py --apply    — save links + clean Shopify

Rotate the Canva template links of affected products afterwards if you think
the old public links were copied.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).parent))
from api_retry import retry_request
from db import LISTINGS_OPTIONAL_COLUMNS, _ensure_optional_columns, get_conn
from listing_builder import strip_template_links
from shopify_common import api_base, headers, normalize_domain

_ROOT       = Path(__file__).resolve().parent.parent
ENV_PATH    = _ROOT / ".env"
OUTPUTS_DIR = _ROOT / "outputs"


def _record_link(product_name: str) -> str:
    """Template link saved by Phase 3B, unless it was one of the old fallbacks."""
    rec = OUTPUTS_DIR / f"{product_name.replace(' ', '_')}_product.json"
    if not rec.exists():
        return ""
    try:
        data = json.loads(rec.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    link = data.get("template_link", "")
    # Older versions fell back to the raw design URL or a preview URL of the
    # original design. Neither lets a buyer make their own copy.
    if not link or link == data.get("design_url") or "mode=preview" in link:
        return ""
    return link


def _clean_html(body_html: str) -> str:
    """Drop the template block that the old uploader added to body_html."""
    marker = body_html.find("✦ HOW TO ACCESS YOUR TEMPLATE")
    if marker != -1:
        cut  = max(body_html.rfind("<p>", 0, marker), 0)
        prev = body_html.rfind("<p>", 0, cut)
        if prev != -1 and "─" in body_html[prev:cut]:
            cut = prev  # include the divider line above the heading
        body_html = body_html[:cut]
    return strip_template_links(body_html)


def run(apply: bool) -> int:
    env    = {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}
    domain = normalize_domain(env.get("SHOPIFY_STORE_DOMAIN", ""))
    token  = env.get("SHOPIFY_ACCESS_TOKEN", "")
    with get_conn() as conn:
        _ensure_optional_columns(conn, "listings", LISTINGS_OPTIONAL_COLUMNS)
        rows = conn.execute(
            "SELECT product_name, shopify_product_id, template_link FROM listings"
        ).fetchall()

    problems = 0
    for row in rows:
        name = row["product_name"]
        link = row["template_link"] or _record_link(name)
        if not link:
            print(f"  [backfill] {name}: no usable template link on file — re-run Phase 3B")
            problems += 1
        elif not row["template_link"]:
            print(f"  [backfill] {name}: template link found in product record")
            if apply:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE listings SET template_link = ? WHERE product_name = ?",
                        (link, name),
                    )

        product_id = row["shopify_product_id"]
        if not (product_id and domain and token):
            continue
        url  = f"{api_base(domain)}/products/{product_id}.json"
        resp = retry_request("GET", url, headers=headers(token),
                             params={"fields": "id,body_html"}, timeout=30)
        if not resp.ok:
            print(f"  [backfill] {name}: could not read product ({resp.status_code})")
            problems += 1
            continue
        body    = resp.json().get("product", {}).get("body_html") or ""
        cleaned = _clean_html(body)
        if cleaned == body:
            continue
        print(f"  [backfill] {name}: public description contains the template link"
              + (" — removing" if apply else " (run with --apply to remove)"))
        if apply:
            put = retry_request("PUT", url, headers=headers(token), timeout=30,
                                json={"product": {"id": int(product_id), "body_html": cleaned}})
            if not put.ok:
                print(f"  [backfill] {name}: update failed ({put.status_code})")
                problems += 1
    return problems


if __name__ == "__main__":
    sys.exit(1 if run(apply="--apply" in sys.argv) else 0)
