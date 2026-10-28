"""
listing_builder.py  —  Phase 4.2
──────────────────────────────────
Reads a queue row from SQLite and formats all fields into a structured
JSON payload that shopify_uploader.py and the validator consume downstream.

Run:
    python scripts/listing_builder.py <product_name>   — uses newest pending row
    python scripts/listing_builder.py <queue_id>       — uses exact row by ID

Output:
    04_Assets/ReadyToUpload/[ProductName]/listing.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from db import get_conn, log_run

_ROOT       = Path(__file__).resolve().parent.parent
ASSETS_DIR  = _ROOT / "04_Assets" / "ReadyToUpload"
INBOX_DIR   = _ROOT / "03_Canva_Exports"


# Older Phase 3B runs appended the template link to the queue description.
# The link must never reach the public product page, so strip that block and
# any stray Canva design links before building the payload.
_TEMPLATE_BLOCK_RE = re.compile(r"\s*─+\s*\n\s*✦ HOW TO ACCESS YOUR TEMPLATE.*\Z", re.DOTALL)
_CANVA_LINK_RE     = re.compile(r"https?://(?:www\.)?canva\.com/\S+")


def strip_template_links(description: str) -> str:
    """Remove any template delivery block or Canva link from a public description."""
    cleaned = _TEMPLATE_BLOCK_RE.sub("", description or "")
    cleaned = _CANVA_LINK_RE.sub("", cleaned)
    return cleaned.rstrip()


def _load_product_extras(product_name: str) -> dict:
    """
    Load template_link and product_pdf from meta.json (written by Phase 3B).
    Returns a dict with those keys (empty strings if not found).
    """
    meta_file = INBOX_DIR / product_name / "meta.json"
    if not meta_file.exists():
        return {"template_link": "", "product_pdf": ""}

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        return {
            "template_link": meta.get("template_link", ""),
            "product_pdf":   meta.get("product_pdf", ""),
        }
    except json.JSONDecodeError as exc:
        print(f"  [4.2] Warning: meta.json is malformed ({meta_file}): {exc}")
        return {"template_link": "", "product_pdf": ""}


def build_listing_payload(queue_id: int) -> dict:
    """
    Reads the queue row and returns a validated payload dict.

    Execution steps:
      1. Fetch the queue row by ID — raises if not found
      2. Parse the tags JSON string back to a list
      3. Load template_link + product_pdf from meta.json (Phase 3B output)
      4. Assemble the full payload
      5. Write listing.json to 04_Assets/ReadyToUpload/[ProductName]/
      6. Return the payload dict for downstream use
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM queue WHERE id = ?", (queue_id,)).fetchone()

    if row is None:
        raise ValueError(f"Queue item {queue_id} not found")

    tags: list[str] = json.loads(row["tags"])
    product_name: str = row["product_name"]

    # Pull in Phase 3B extras (template link + PDF path)
    extras = _load_product_extras(product_name)
    if extras["template_link"]:
        print("  [4.2] Template link found — stored for post-purchase delivery")
    else:
        print("  [4.2] Warning: no template link yet — Phase 5 will not publish this product")
    if extras["product_pdf"]:
        print(f"  [4.2] Product PDF found: {Path(extras['product_pdf']).name}")

    # The public description never carries the template link; buyers get it by email
    description = strip_template_links(row["description"])

    payload = {
        "queue_id":      queue_id,
        "product_name":  product_name,
        "title":         row["title"],
        "tags":          tags,
        "description":   description,
        "price":         row["price"],
        "category":      row["category"],
        "template_link": extras["template_link"],  # private: delivered after payment
        "digital_file":  extras["product_pdf"],    # local PDF backup of the product
    }

    out_dir = ASSETS_DIR / product_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "listing.json"
    out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  [4.2] Listing payload written: {out_file}")
    log_run(product_name, "listing_builder", "success", f"Payload written to {out_file}")
    return payload


def _resolve_queue_id(arg: str) -> int:
    """Accept either a numeric queue_id or a product_name string."""
    if arg.isdigit():
        return int(arg)
    # Look up the most recent pending row for this product name
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM queue WHERE product_name = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (arg,),
        ).fetchone()
    if row is None:
        raise ValueError(f"No pending queue item found for product: {arg}")
    return row["id"]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/listing_builder.py <product_name|queue_id>")
        sys.exit(1)

    try:
        qid = _resolve_queue_id(sys.argv[1])
        p = build_listing_payload(qid)
        print(f"\n  Done — payload for: {p['product_name']}")
    except ValueError as e:
        print(f"[error] {e}")
        sys.exit(1)
