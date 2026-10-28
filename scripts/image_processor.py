"""
image_processor.py  —  Phase 4.1
──────────────────────────────────
Renames mockups to the storefront naming convention, resizes to 2000×2000 px,
and converts to JPEG. Called by n8n Execute Command node after Canva
exports files to 02_Products/[ProductName]/Mockups/.

Run:
    python scripts/image_processor.py <product_name>

Requires:
    pip install Pillow
"""

from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image
from db import log_run

_ROOT       = Path(__file__).resolve().parent.parent
PRODUCTS_DIR = _ROOT / "02_Products"
TARGET_SIZE  = (2000, 2000)
JPEG_QUALITY = 92


def _store_filename(product_name: str, index: int) -> str:
    """Return a URL-safe filename: lowercase, hyphens, 1-indexed."""
    slug = product_name.lower().replace(" ", "-")
    return f"{slug}-mockup-{index:02d}.jpg"


def process_mockups(product_name: str) -> list[Path]:
    """
    Resize and rename all images in 02_Products/[ProductName]/Mockups/.
    Returns list of output file paths.

    Execution steps:
      1. Locate source images (PNG, JPG, JPEG, WEBP) in Mockups/
      2. Sort by filename for consistent 01–05 numbering
      3. Open each image with Pillow
      4. Resize to 2000×2000 using LANCZOS — preserves detail at the storefront's
         thumbnail scale without upscaling artifacts
      5. Convert to RGB (strips alpha channel; JPEG has no alpha channel)
      6. Save as JPEG at quality=92 — good visual quality under typical web
         500 KB per-image limit
      7. Remove the original source file to keep the folder clean
    """
    mockups_dir = PRODUCTS_DIR / product_name / "Mockups"
    if not mockups_dir.exists():
        raise FileNotFoundError(f"Mockups directory not found: {mockups_dir}")

    source_images = sorted(
        f for f in mockups_dir.iterdir()
        if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        and not f.name.startswith(".")
    )

    if not source_images:
        raise ValueError(f"No images found in {mockups_dir}")

    output_paths: list[Path] = []

    for i, src in enumerate(source_images, start=1):
        out_name = _store_filename(product_name, i)
        out_path = mockups_dir / out_name

        with Image.open(src) as img:
            # Use LANCZOS for Pillow < 10.0, Resampling.LANCZOS for >= 10.0
            try:
                resampling = Image.Resampling.LANCZOS  # type: ignore[attr-defined]
            except AttributeError:
                resampling = Image.LANCZOS  # type: ignore[attr-defined]

            resized = img.resize(TARGET_SIZE, resampling)
            resized.convert("RGB").save(out_path, "JPEG", quality=JPEG_QUALITY)

        if src != out_path:
            src.unlink()

        output_paths.append(out_path)
        print(f"  [4.1] Processed: {out_name} ({TARGET_SIZE[0]}×{TARGET_SIZE[1]}px, JPEG)")

    return output_paths


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/image_processor.py <product_name>")
        sys.exit(1)

    product = sys.argv[1]
    try:
        paths = process_mockups(product)
        log_run(product, "image_processor", "success", f"{len(paths)} images processed")
        print(f"\n  Done — {len(paths)} images processed for: {product}")
    except (FileNotFoundError, ValueError) as e:
        log_run(product, "image_processor", "failed", str(e))
        print(f"[error] {e}")
        sys.exit(1)
