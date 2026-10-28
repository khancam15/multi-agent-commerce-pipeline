"""
canva_image_generator.py — Phase 3
────────────────────────────────────
Fully autonomous Canva image generator. Reads listing content from
outputs/master.txt, generates 6 conversion-optimised product
images via Anthropic API + Canva remote MCP, exports each as a JPEG,
downloads them to 02_Products/[ProductName]/Mockups/, and queues the
product for Phase 4.

After this script exits 0, the product is in the queue with images
on disk — Phase 4 can run immediately in the same loop cycle.

Image slots (conversion strategy):
  1 — Before/After hero    (stop the scroll — highest CTR format)
  2 — Features + price     (value proof — price anchoring)
  3 — Filled-in preview    (remove doubt — show exactly what they get)
  4 — How it works         (remove friction — 3-step, time-specific)
  5 — Trust signals        (badges + factual product benefits)
  6 — Bundle value stack   (close the sale — perceived discount)

Run:
    python scripts/canva_image_generator.py "Product Name" [--price 9.99]

Requires:
    ANTHROPIC_API_KEY in .env
    CANVA_MCP_TOKEN in .env   (Canva OAuth bearer token — see README)
                               On first run without a token, the script will
                               print an auth URL. Visit it once, then add the
                               token to .env as: CANVA_MCP_TOKEN=Bearer <token>
    CANVA_ACCESS_TOKEN in .env (set by canva_oauth.py — used for Connect API exports)
    CANVA_REFRESH_TOKEN in .env

Export strategy:
    MCP call → generates designs in Canva, returns canva.com/design/... URLs
    CanvaClient.export_from_url() → Canva Connect API export job → JPEG download URLs
    This two-stage approach is more reliable than parsing download URLs from MCP text.

Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, TypedDict, cast

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

try:
    import requests
except ImportError:
    print("[3] ERROR: requests not installed. Run: .venv/bin/pip install requests")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from db import insert_queue_item, title_exists, log_run
from meta_generator import (
    generate as write_meta,
    list_products as _list_products,
    MASTER_FILE,
)
from canva_api import CanvaClient, CanvaAPIError

OUTPUTS_DIR  = _ROOT / "outputs"
INBOX_DIR    = _ROOT / "03_Canva_Exports"
PRODUCTS_DIR = _ROOT / "02_Products"
BRAND_GUIDE  = OUTPUTS_DIR / "brand_guide.md"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CANVA_MCP_URL = "https://mcp.canva.com/mcp"
MODEL         = os.getenv("CANVA_MODEL", "claude-opus-4-5")


class SlotSpec(TypedDict):
    slot: int
    role: str
    desc: str


class ImageEntry(TypedDict):
    slot: int
    role: str
    design_url: str
    export_url: str

# ── 6-slot conversion strategy ─────────────────────────────────────────────────
SLOTS: list[SlotSpec] = [
    {
        "slot": 1,
        "role": "Before/After split hero",
        "desc": (
            "Split-screen layout. LEFT SIDE labelled 'BEFORE': a plain, unformatted document. "
            "RIGHT SIDE labelled 'AFTER': the same content laid out in the template — "
            "polished and professional. "
            "Bold 'BEFORE → AFTER' label spanning the centre split. Large headline across the top: "
            "'[PRODUCT] — Look Professional in Minutes'. "
            "Show the design change only; do not promise results such as deals, jobs or income."
        ),
    },
    {
        "slot": 2,
        "role": "Features + price anchor",
        "desc": (
            "Clean feature list layout. 4–5 checkmark bullet points, each naming a real feature "
            "or benefit of the product: '✓ Fully editable in Canva', "
            "'✓ Professional layout ready to fill in', '✓ Works on desktop and mobile'. "
            "Do not promise outcomes such as landing deals, clients or income. "
            "Bold price anchor at the bottom: "
            "'Hiring a designer: $200+  |  Yours today: $[PRICE]'. "
            "Premium minimal look with generous white space."
        ),
    },
    {
        "slot": 3,
        "role": "Filled-in template preview",
        "desc": (
            "Full mockup of the template filled in with realistic sample data — "
            "real-looking creator name, photo placeholder, portfolio stats, brand logos. "
            "Show the actual quality buyers are downloading. "
            "Overlay caption: 'Fully editable in Canva — ready in 5 minutes'. "
            "Removes purchase hesitation by showing exactly what is included."
        ),
    },
    {
        "slot": 4,
        "role": "How it works — 3 steps",
        "desc": (
            "Three numbered steps with simple icons: "
            "1️⃣ Get your Canva link by email after purchase  "
            "2️⃣ Add your name, photos, and details in Canva  "
            "3️⃣ Download or share your finished design. "
            "Short line below the steps: 'Edit in minutes, no design skills needed'."
        ),
    },
    {
        "slot": 5,
        "role": "Trust badges + product facts",
        "desc": (
            "Badge row: 'Beginner Friendly' | 'Delivered by Email' | 'Canva Compatible' | "
            "'5-Min Setup'. "
            "Below it, three short factual benefits drawn from the product listing. "
            "Do not invent testimonials, quotes, reviews, star ratings, or customer results."
        ),
    },
    {
        "slot": 6,
        "role": "Bundle value stack",
        "desc": (
            "Stacked value list — each item in the product shown with its individual value. "
            "Total value line: 'Total value: $[TOTAL]'. "
            "Large visual strikethrough of the total. "
            "Bold large text: 'Yours today: $[PRICE]'. "
            "Creates urgency via price anchoring and perceived 80%+ discount."
        ),
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_brand_context() -> str:
    if not BRAND_GUIDE.exists():
        return (
            f"Brand: {os.getenv('BRAND_NAME', 'Your Brand')}. "
            "Colors: cream #faf7f2 backgrounds, blush pink #c4a0a0 accents. "
            "Serif headlines, clean sans body, minimal editorial layout."
        )
    text   = BRAND_GUIDE.read_text(encoding="utf-8")
    lines: list[str] = []
    in_sec = False
    for line in text.split("\n"):
        lo = line.lower()
        if any(k in lo for k in ["color", "font", "palette", "typography", "visual", "aesthetic", "brand voice"]):
            in_sec = True
        if in_sec:
            lines.append(line)
        if in_sec and not line.strip() and len(lines) > 6:
            break
    return "\n".join(lines[:30]) or text[:600]


def _get_price_from_master(product_name: str) -> float:
    if not MASTER_FILE.exists():
        return 9.99
    products = _list_products(MASTER_FILE.read_text(encoding="utf-8"))
    for p in products:
        if p["name"].lower() == product_name.lower():
            return float(p["price"])
    return 9.99


def _build_prompt(
    product_name: str,
    price: float,
    title: str,
    tags: list[str],
    description: str,
    brand_ctx: str,
) -> str:
    total_value = round(price * 19, 2)
    slot_blocks = "\n\n".join(
        "**IMAGE {slot} — {role}**\n{desc}".format(
            slot=s["slot"],
            role=s["role"],
            desc=s["desc"]
            .replace("[PRODUCT]", product_name)
            .replace("[PRICE]", str(price))
            .replace("[TOTAL]", str(total_value)),
        )
        for s in SLOTS
    )

    return f"""You are generating 6 product mockup images for a digital product using Canva, then exporting each one.

PRODUCT:
  Name:        {product_name}
  Listing title:  {title}
  Price:       ${price}
  Keywords:    {", ".join(tags[:8])}
  Description: {description[:250]}

BRAND VISUAL RULES:
{brand_ctx}
Core palette: cream #faf7f2 backgrounds, blush pink #c4a0a0 accents, dark readable text.
Style: serif headlines, clean sans body, generous white space, minimal editorial feel.

YOUR STEPS — execute fully in order:
1. Create a Canva folder named exactly: "{product_name}"
2. For each of the 6 images below, generate one instagram_post design (1080×1350 portrait)
3. Move each design into the "{product_name}" folder
4. After each image is done, output EXACTLY in this format (so it can be parsed):
   IMAGE_[N]_DESIGN: https://www.canva.com/design/...

DESIGN RULES (apply to all 6):
- Format: instagram_post — 1080×1350 portrait (product page optimal)
- Cream #faf7f2 background, blush #c4a0a0 accents, dark text for readability
- Serif headlines, clean sans body, generous white space, premium finish
- Include product name and ${price} where relevant to each image's purpose
- Each image has exactly one conversion job — do not mix purposes

IMAGE SPECIFICATIONS:

{slot_blocks}

Generate all 6 images now. Output each IMAGE_[N]_DESIGN line as you finish each one."""


def _call_api(prompt: str) -> dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

    canva_token = os.getenv("CANVA_MCP_TOKEN", "")
    mcp_server: dict[str, Any] = {"type": "url", "url": CANVA_MCP_URL, "name": "canva"}
    if canva_token:
        mcp_server["authorization_token"] = canva_token

    resp = requests.post(
        ANTHROPIC_URL,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta":    "mcp-client-2025-04-04",
        },
        json={
            "model":       MODEL,
            "max_tokens":  8192,
            "messages":    [{"role": "user", "content": prompt}],
            "mcp_servers": [mcp_server],
        },
        timeout=600,
    )

    if resp.status_code == 401:
        try:
            body: dict[str, Any] = resp.json()
        except ValueError:
            body = {}
        err_obj = body.get("error")
        err_map: dict[str, Any] = cast(dict[str, Any], err_obj) if isinstance(err_obj, dict) else {}
        auth_url_obj = err_map.get("auth_url") or body.get("auth_url")
        auth_url = auth_url_obj if isinstance(auth_url_obj, str) else ""
        if auth_url:
            raise RuntimeError(
                f"\n  Canva requires authorisation. Visit this URL once in your browser:\n\n"
                f"  {auth_url}\n\n"
                f"  After authorising, copy the access token and add it to .env:\n"
                f"  CANVA_MCP_TOKEN=Bearer <your_token>\n"
                f"  Then re-run.\n"
            )
        raise RuntimeError(f"API 401: {body or resp.text[:200]}")

    resp.raise_for_status()
    return resp.json()


def _collect_text(response: dict[str, Any]) -> str:
    """Flatten all text content from the API response into one string."""
    parts: list[str] = []
    content_obj = response.get("content", [])
    if not isinstance(content_obj, list):
        return ""
    content = cast(list[Any], content_obj)
    for block_obj in content:
        if not isinstance(block_obj, dict):
            continue
        block = cast(dict[str, Any], block_obj)
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        elif btype == "mcp_tool_result":
            sub_content_obj = block.get("content", [])
            if not isinstance(sub_content_obj, list):
                continue
            sub_content = cast(list[Any], sub_content_obj)
            for sub_obj in sub_content:
                if not isinstance(sub_obj, dict):
                    continue
                sub = cast(dict[str, Any], sub_obj)
                if sub.get("type") == "text":
                    sub_text = sub.get("text")
                    if isinstance(sub_text, str):
                        parts.append(sub_text)
    return "\n".join(parts)


def _parse_image_urls(text: str) -> list[ImageEntry]:
    """
    Parse structured IMAGE_N_DESIGN / IMAGE_N_EXPORT lines from the response.
    Falls back to scanning for canva.com/design/ URLs if structured lines aren't found.
    """
    results: dict[int, dict[str, str]] = {}

    # Primary: structured format
    design_re = re.compile(r"IMAGE_(\d+)_DESIGN:\s*(https://[^\s]+)", re.IGNORECASE)
    export_re  = re.compile(r"IMAGE_(\d+)_EXPORT:\s*(https://[^\s]+)", re.IGNORECASE)

    for m in design_re.finditer(text):
        n = int(m.group(1))
        results.setdefault(n, {})["design_url"] = m.group(2).strip()

    for m in export_re.finditer(text):
        n = int(m.group(1))
        results.setdefault(n, {})["export_url"] = m.group(2).strip()

    # Fallback: just collect canva design URLs in order
    if not results:
        canva_re = re.compile(r"https://www\.canva\.com/design/[^\s\"')>\]]+")
        for i, url in enumerate(dict.fromkeys(canva_re.findall(text)), start=1):
            results[i] = {"design_url": url}

    # Also pick up any export/download URLs not yet captured
    export_url_re = re.compile(
        r"https://(?:document-export|export|cdn)\.canva\.com/[^\s\"')>\]]+"
    )
    extra_exports = list(dict.fromkeys(export_url_re.findall(text)))
    for i, url in enumerate(extra_exports, start=1):
        if i in results and "export_url" not in results[i]:
            results[i]["export_url"] = url

    # Build ordered list up to 6
    output: list[ImageEntry] = []
    for slot_idx in range(1, 7):
        entry: dict[str, str] = results.get(slot_idx, {})
        if entry.get("design_url") or entry.get("export_url"):
            output.append({
                "slot":       slot_idx,
                "role":       SLOTS[slot_idx - 1]["role"] if slot_idx <= len(SLOTS) else f"Image {slot_idx}",
                "design_url": entry.get("design_url", ""),
                "export_url": entry.get("export_url", ""),
            })

    return output


def _export_via_api(image_data: list[ImageEntry]) -> list[ImageEntry]:
    """
    For each image entry that has a design_url but no reliable export_url,
    call the Canva Connect API to generate a proper JPEG export URL.

    Returns an updated copy of image_data with export_url filled in
    wherever the API export succeeded.
    """
    # Check if we have Canva Connect API credentials
    if not os.getenv("CANVA_ACCESS_TOKEN"):
        print("  [3] CANVA_ACCESS_TOKEN not set — skipping API export, will use MCP URLs")
        return image_data

    try:
        client = CanvaClient()
    except CanvaAPIError as exc:
        print(f"  [3] Canva API init failed: {exc} — falling back to MCP export URLs")
        return image_data

    updated: list[ImageEntry] = []
    for img in image_data:
        entry: ImageEntry = {
            "slot": img["slot"],
            "role": img["role"],
            "design_url": img["design_url"],
            "export_url": img["export_url"],
        }
        design_url = entry["design_url"]

        # Skip if we already have a working export URL from MCP
        # (export_url is only set if MCP returned a structured IMAGE_N_EXPORT line)
        if entry["export_url"] and "canva.com" not in entry["export_url"]:
            # Already a direct CDN URL — keep it
            updated.append(entry)
            continue

        if not design_url:
            print(f"  [3] Slot {entry['slot']}: no design URL — skipping API export")
            updated.append(entry)
            continue

        try:
            print(f"  [3] Slot {entry['slot']}: exporting via Canva Connect API...")
            urls = client.export_from_url(design_url, fmt="jpg", quality=92)
            if urls:
                entry["export_url"] = urls[0]  # single-page design → first URL
                print(f"  [3] Slot {entry['slot']}: export URL obtained ({urls[0][:70]}...)")
            else:
                print(f"  [3] Slot {entry['slot']}: API export returned no URLs")
        except CanvaAPIError as exc:
            print(f"  [3] Slot {entry['slot']}: API export failed: {exc} — will try MCP URL")

        updated.append(entry)

    return updated


def _download_images(product_name: str, image_data: list[ImageEntry]) -> list[Path]:
    """
    Download exported JPEGs to 02_Products/[ProductName]/Mockups/.

    Strategy:
      1. Try Canva Connect API export for each design (reliable authenticated URLs)
      2. Fall back to export_url from MCP response if API export is unavailable
      3. Fall back to design_url as last resort

    Returns list of saved file paths.
    """
    mockups_dir = PRODUCTS_DIR / product_name / "Mockups"
    mockups_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Enrich image_data with API export URLs where possible
    image_data = _export_via_api(image_data)

    saved: list[Path] = []
    for img in image_data:
        url = img.get("export_url") or img.get("design_url")
        if not url:
            print(f"  [3] WARNING: No download URL for slot {img['slot']} — skipping")
            continue

        filename = (
            f"mockup_{img['slot']:02d}_"
            f"{img['role'].lower().replace(' ', '_').replace('/', '_')[:30]}.jpg"
        )
        dest = mockups_dir / filename

        try:
            r = requests.get(url, timeout=120, allow_redirects=True)
            r.raise_for_status()

            # Verify we got image data, not an HTML auth redirect
            content_type = r.headers.get("content-type", "")
            if "text/html" in content_type and len(r.content) < 10_000:
                print(
                    f"  [3] WARNING: Slot {img['slot']} returned HTML (auth redirect?) — "
                    f"skipping. Check CANVA_ACCESS_TOKEN in .env"
                )
                continue

            dest.write_bytes(r.content)
            saved.append(dest)
            print(f"  [3] Downloaded: {filename} ({len(r.content) // 1024} KB)")

        except requests.HTTPError as exc:
            print(f"  [3] Download failed for slot {img['slot']}: {exc} — skipping")
        except Exception as exc:
            print(f"  [3] Download error for slot {img['slot']}: {exc} — skipping")

    return saved


def _ensure_queued(product_name: str, meta: dict[str, Any]) -> int | None:
    """
    Add product to the queue if the title hasn't been published before.
    Returns queue_id on insert, None if already exists.
    """
    if title_exists(meta["title"]):
        print(f"  [3] '{meta['title'][:60]}...' already in listings — skipping queue insert")
        return None

    queue_id = insert_queue_item(
        product_name=product_name,
        title=meta["title"],
        tags=meta["tags"],
        description=meta["description"],
        price=float(meta["price"]),
        category=meta.get("category", "Digital Downloads"),
    )
    print(f"  [3] Queued: {product_name} (queue ID: {queue_id})")
    return queue_id


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_images(product_name: str, price: float | None = None) -> int:
    """
    Full autonomous Phase 3:
      1. Write meta.json (listing data)
      2. Generate 6 Canva designs + export as JPEG
      3. Download JPEGs to 02_Products/[ProductName]/Mockups/
      4. Insert product into queue
    Returns 0 on success, 1 on failure.
    """
    print("\n  [3] ── Canva Image Generator ──────────────────────────")
    print(f"  [3] Product: {product_name}")

    # Resolve price
    if price is None:
        price = _get_price_from_master(product_name)
    print(f"  [3] Price:   ${price}")

    # Write meta.json (listing data for watcher + queue)
    try:
        write_meta(product_name, price)
    except Exception as exc:
        print(f"  [3] WARNING: meta.json write failed: {exc}")

    # Load listing data for prompt
    meta_file = INBOX_DIR / product_name / "meta.json"
    meta: dict[str, Any]
    if meta_file.exists():
        loaded_meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta = cast(dict[str, Any], loaded_meta) if isinstance(loaded_meta, dict) else {}
        title_obj = meta.get("title", product_name)
        tags_obj = meta.get("tags", [])
        description_obj = meta.get("description", "")
        title = title_obj if isinstance(title_obj, str) else product_name
        description = description_obj if isinstance(description_obj, str) else ""
        tags_raw = cast(list[Any], tags_obj) if isinstance(tags_obj, list) else []
        tags = [str(t) for t in tags_raw]
    else:
        title       = product_name
        tags = []
        description = ""
        meta        = {
            "product_name": product_name,
            "title":        title,
            "tags":         tags,
            "description":  description,
            "price":        price,
            "category":     "Digital Downloads",
        }

    brand_ctx = _load_brand_context()
    prompt    = _build_prompt(product_name, price, title, tags, description, brand_ctx)

    print(f"  [3] Calling Anthropic API + Canva MCP ({MODEL})")
    print("  [3] Step 1: Generating 6 Canva designs via MCP — estimated 2–4 minutes...")
    print("  [3] Step 2: Exporting JPEGs via Canva Connect API (runs after MCP)")

    try:
        response = _call_api(prompt)
    except RuntimeError as exc:
        print(f"  [3] ERROR: {exc}")
        log_run(product_name, "canva_image_generator", "failed", str(exc)[:400])
        return 1
    except requests.HTTPError as exc:
        body = getattr(exc.response, "text", str(exc))[:400]
        print(f"  [3] HTTP error: {body}")
        log_run(product_name, "canva_image_generator", "failed", body)
        return 1
    except Exception as exc:
        print(f"  [3] Unexpected error: {exc}")
        log_run(product_name, "canva_image_generator", "failed", str(exc)[:400])
        return 1

    # Parse response
    text       = _collect_text(response)
    image_data = _parse_image_urls(text)

    if not image_data:
        debug_path = OUTPUTS_DIR / f"{product_name.replace(' ', '_')}_canva_raw.json"
        debug_path.write_text(json.dumps(response, indent=2), encoding="utf-8")
        print(f"  [3] ERROR: No image URLs found. Raw response → {debug_path}")
        log_run(product_name, "canva_image_generator", "failed",
                f"No image URLs — debug saved to {debug_path.name}")
        return 1

    print(f"\n  [3] {len(image_data)} designs generated:")
    for img in image_data:
        print(f"       Image {img['slot']} — {img['role']}")
        if img["design_url"]:
            print(f"         Canva: {img['design_url']}")
        if img["export_url"]:
            print(f"         Export: {img['export_url'][:80]}...")

    # Download JPEGs to 02_Products/[ProductName]/Mockups/
    print(f"\n  [3] Downloading JPEGs to 02_Products/{product_name}/Mockups/...")
    saved_files = _download_images(product_name, image_data)

    if len(saved_files) < 5:
        print(
            f"  [3] WARNING: Only {len(saved_files)} images downloaded "
            f"(need 5 minimum for Phase 4). "
        )
        if not saved_files:
            print(
                "  [3] Export download failed — images are in Canva. "
                "Export manually to Google Drive and the watcher will queue them."
            )
            # Still save design URLs and queue with Canva URLs for tracking
            log_run(product_name, "canva_image_generator", "failed",
                    f"Download failed — {len(saved_files)} of {len(image_data)} images saved")
            # Don't queue — Phase 4 needs images on disk
            return 1

    # Save design record
    OUTPUTS_DIR.mkdir(exist_ok=True)
    record: dict[str, Any] = {
        "product_name": product_name,
        "price":        price,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "images":       image_data,
        "downloaded":   [str(p) for p in saved_files],
    }
    safe_name = product_name.replace(" ", "_")
    out_path  = OUTPUTS_DIR / f"{safe_name}_canva.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [3] Record saved → {out_path}")

    # Insert into queue
    queue_id = _ensure_queued(product_name, meta)

    if queue_id is None:
        print("  [3] Product already in listings — skipping re-queue.")
        # Still a success — images are downloaded
        log_run(product_name, "canva_image_generator", "success",
                f"{len(saved_files)} images downloaded — already in listings, not re-queued")
        return 0

    print(f"\n  [3] ✓ Phase 3 complete: {len(saved_files)} images + queue ID {queue_id}")
    print("  [3] Pipeline will continue to Phase 4 → 5 → 6 automatically.")

    log_run(product_name, "canva_image_generator", "success",
            f"{len(saved_files)} images downloaded, queue_id={queue_id}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage: python scripts/canva_image_generator.py \"Product Name\" [--price 9.99]")
        sys.exit(1)

    _product = args[0]
    _price: float | None = None
    if "--price" in args:
        idx = args.index("--price")
        if idx + 1 < len(args):
            _price = float(args[idx + 1])

    sys.exit(generate_images(_product, _price))
