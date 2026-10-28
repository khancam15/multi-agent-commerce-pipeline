"""
pre_upload_validator.py  —  Phase 4.3
───────────────────────────────────────
Validates a listing payload and its mockup images against the store's rules
before handing off to stripe_publisher. If validation fails, writes the
error to run_log and halts the run cleanly.

Run:
    python scripts/pre_upload_validator.py <product_name>

Exits 0 on pass, exits 1 on failure.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from db import log_run

_ROOT      = Path(__file__).resolve().parent.parent
ASSETS_DIR = _ROOT / "04_Assets" / "ReadyToUpload"

# Store limits
TITLE_MAX_CHARS    = 255
TAG_COUNT_MIN      = 1
TAG_COUNT_MAX      = 250
TAG_MAX_CHARS      = 255
DESC_MIN_CHARS     = 10
PRICE_MIN          = 0.01
PRICE_MAX          = 99999.00
REQUIRED_IMAGES    = 1


@dataclass
class ValidationResult:
    passed: bool = True
    errors: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.passed = False
        self.errors.append(msg)


def validate(product_name: str) -> ValidationResult:
    """
    Validates listing.json and mockup images for a product.

    Checks (store rules):
      - Title ≤ 255 characters
      - 1–250 tags, each ≤ 255 characters
      - Description present (≥ 10 characters)
      - Price > $0.01
      - At least 1 mockup JPEG present
    """
    result = ValidationResult()
    product_dir = ASSETS_DIR / product_name
    payload_file = product_dir / "listing.json"

    if not payload_file.exists():
        result.fail(f"listing.json not found at {payload_file}")
        return result

    try:
        payload = json.loads(payload_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.fail(f"listing.json is malformed: {exc}")
        return result

    if not isinstance(payload, dict):
        result.fail("listing.json must be a JSON object")
        return result

    # Title
    title = payload.get("title", "")
    if len(title) > TITLE_MAX_CHARS:
        result.fail(f"Title too long: {len(title)} chars (max {TITLE_MAX_CHARS})")
    if not title.strip():
        result.fail("Title is empty")

    # Tags
    tags_raw = payload.get("tags", [])
    if not isinstance(tags_raw, list):
        result.fail(f"tags must be a list, got {type(tags_raw).__name__}")
        tags: list[str] = []
    else:
        tags = [str(t) for t in tags_raw]
    if not (TAG_COUNT_MIN <= len(tags) <= TAG_COUNT_MAX):
        result.fail(f"Tag count: {len(tags)} (must be {TAG_COUNT_MIN}–{TAG_COUNT_MAX})")
    long_tags = [t for t in tags if len(t) > TAG_MAX_CHARS]
    if long_tags:
        result.fail(f"Tags exceed {TAG_MAX_CHARS} chars: {long_tags[:3]}")

    # Description
    desc = payload.get("description", "")
    if len(desc) < DESC_MIN_CHARS:
        result.fail(f"Description too short: {len(desc)} chars (min {DESC_MIN_CHARS})")

    # Price
    try:
        price = float(payload.get("price", 0))
    except (TypeError, ValueError):
        result.fail(f"price is not a valid number: {payload.get('price')!r}")
        price = 0.0
    if not (PRICE_MIN <= price <= PRICE_MAX):
        result.fail(f"Price ${price} outside the allowed range (${PRICE_MIN}\u2013${PRICE_MAX})")

    # Images — look in Mockups/ or the product ReadyToUpload dir
    mockups_dir  = _ROOT / "02_Products" / product_name / "Mockups"
    staged_dir   = ASSETS_DIR / product_name
    jpeg_images  = list(mockups_dir.glob("*.jpg")) if mockups_dir.exists() else []
    if not jpeg_images:
        jpeg_images = list(staged_dir.glob("*.jpg"))
    if len(jpeg_images) < REQUIRED_IMAGES:
        result.fail(
            f"Only {len(jpeg_images)} JPEG mockup(s) found for {product_name} "
            f"(need at least {REQUIRED_IMAGES})"
        )

    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/pre_upload_validator.py <product_name>")
        sys.exit(1)

    product = sys.argv[1]
    result = validate(product)

    if result.passed:
        print(f"  [4.3] Validation passed for: {product}")
        log_run(product, "validator", "success", "All checks passed")
        sys.exit(0)
    else:
        for err in result.errors:
            print(f"  [4.3] FAIL: {err}")
        log_run(product, "validator", "failed", " | ".join(result.errors))
        sys.exit(1)
