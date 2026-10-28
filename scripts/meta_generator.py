"""
meta_generator.py — Auto-generate meta.json from master.txt
─────────────────────────────────────────────────────────────
Reads outputs/master.txt, finds the matching product, and writes
meta.json to 03_Canva_Exports/[ProductName]/ ready for the watcher.

Run:
    python scripts/meta_generator.py "UGC Rate Card Template" --price 7.99
    python scripts/meta_generator.py --list    ← show all products in master.txt

After running, drop your 5 Canva mockup JPEGs into the same folder
in Dropbox/Google Drive. The watcher handles the rest.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT        = Path(__file__).resolve().parent.parent
MASTER_FILE  = _ROOT / "outputs" / "master.txt"
INBOX_DIR    = _ROOT / "03_Canva_Exports"

SEO_TAGS_RE  = re.compile(
    r"SEO TAGS.*?:\s*\n((?:[^\n]+,?\n?)+?)(?:\n\n|\Z)", re.IGNORECASE | re.DOTALL
)
PRODUCT_RE   = re.compile(r"\d+\.\s+(.+?)\s+\(\$(\d+(?:\.\d{2})?)\)")
TITLE_RE     = re.compile(r"\*\*Title\*\*[:\s]+(.{10,140})")
TAGS_RE      = re.compile(r"\*\*Tags\*\*[:\s]*((?:[^\n]+\n?){1,3})")
DESC_RE      = re.compile(r"\*\*(?:Full )?Description\*\*[:\s]*([\s\S]{100,1500}?)(?=\n\*\*|\Z)")


def _global_tags(master_text: str) -> list[str]:
    """Extract the global SEO tag list near the top of master.txt."""
    m = SEO_TAGS_RE.search(master_text)
    if not m:
        return []
    raw = m.group(1).replace("\n", ",")
    tags = [t.strip().strip(",") for t in raw.split(",") if t.strip()]
    return tags[:250]


def list_products(master_text: str) -> list[dict[str, str]]:
    return [
        {"name": m.group(1).strip(), "price": m.group(2)}
        for m in PRODUCT_RE.finditer(master_text)
    ]


def find_product_section(master_text: str, product_name: str) -> str:
    """Return the section of master.txt closest to the product name."""
    idx = master_text.lower().find(product_name.lower())
    if idx == -1:
        return ""
    return master_text[max(0, idx - 200): idx + 3000]


def generate(product_name: str, price: float) -> Path:
    if not MASTER_FILE.exists():
        raise FileNotFoundError(
            "outputs/master.txt not found — run ./run.sh phase2 first"
        )

    master_text = MASTER_FILE.read_text(encoding="utf-8")
    section     = find_product_section(master_text, product_name)
    global_tags = _global_tags(master_text)

    # Try to extract a specific title from the section
    title = ""
    tm = TITLE_RE.search(section)
    if tm:
        title = tm.group(1).strip()[:255]
    if not title:
        # Build a sensible default title from the product name
        title = f"{product_name} | Editable Canva Template | Digital Download for Creators"[:255]

    # Try to extract tags from the section; fall back to global tags
    tags: list[str] = []
    tgm = TAGS_RE.search(section)
    if tgm:
        raw = tgm.group(1).replace("\n", ",")
        tags = [t.strip().strip(",*").lower() for t in raw.split(",") if t.strip()][:250]
    if len(tags) < 13:
        tags = (tags + global_tags)[:250]

    # Description
    description = ""
    dm = DESC_RE.search(section)
    if dm:
        description = dm.group(1).strip()[:2000]
    if not description:
        description = (
            f"Digital product: professionally designed {product_name}. "
            "Fully editable in Canva. No design skills required. "
            "Perfect for freelancers and creators."
        )

    payload = {
        "product_name": product_name,
        "title":        title,
        "tags":         tags,
        "description":  description,
        "price":        price,
        "category":     "Digital Downloads",
    }

    out_dir = INBOX_DIR / product_name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "meta.json"
    out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  [meta_generator] Written: {out_file}")
    print(f"  Title:  {title[:80]}...")
    print(f"  Tags:   {', '.join(tags[:5])}...")
    print(f"  Price:  ${price}")
    return out_file


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--list" in args:
        if not MASTER_FILE.exists():
            print("[error] outputs/master.txt not found — run ./run.sh phase2 first")
            sys.exit(1)
        products = list_products(MASTER_FILE.read_text(encoding="utf-8"))
        if not products:
            print("No products found in master.txt")
        else:
            print("\n  Products in master.txt:")
            for p in products:
                print(f"    ${p['price']:>6}  {p['name']}")
        sys.exit(0)

    if len(args) < 1:
        print("Usage:")
        print("  python scripts/meta_generator.py \"Product Name\" --price 9.99")
        print("  python scripts/meta_generator.py --list")
        sys.exit(1)

    name  = args[0]
    price = 7.99  # default

    if "--price" in args:
        idx = args.index("--price")
        if idx + 1 < len(args):
            price = float(args[idx + 1])

    try:
        generate(name, price)
    except FileNotFoundError as e:
        print(f"[error] {e}")
        sys.exit(1)
