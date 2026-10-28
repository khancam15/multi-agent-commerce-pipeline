"""
file_organizer.py  —  Phase 4.5
──────────────────────────────────
Moves validated files (listing.json + mockup images) into
04_Assets/ReadyToUpload/[ProductName]/ so stripe_publisher has a
single clean source directory to read from.

Run:
    python scripts/file_organizer.py <product_name>
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from db import log_run

_ROOT         = Path(__file__).resolve().parent.parent
PRODUCTS_DIR  = _ROOT / "02_Products"
READY_DIR     = _ROOT / "04_Assets" / "ReadyToUpload"


def organize(product_name: str) -> Path:
    """
    Copies mockup JPEGs from 02_Products/[ProductName]/Mockups/
    into 04_Assets/ReadyToUpload/[ProductName]/.
    listing.json is already there from listing_builder.py.

    Execution steps:
      1. Verify source mockups directory exists
      2. Create the ReadyToUpload product directory if absent
      3. Copy each JPEG mockup across (copy, not move — keeps originals
         in 02_Products/ until archived after publish)
      4. Log success to run_log
    """
    mockups_dir = PRODUCTS_DIR / product_name / "Mockups"
    ready_dir   = READY_DIR / product_name
    ready_dir.mkdir(parents=True, exist_ok=True)

    if not mockups_dir.exists():
        raise FileNotFoundError(f"Mockups dir not found: {mockups_dir}")

    jpegs = sorted(mockups_dir.glob("*.jpg"))
    if not jpegs:
        raise ValueError(f"No JPEG mockups found in {mockups_dir}")

    for src in jpegs:
        dest = ready_dir / src.name
        shutil.copy2(src, dest)
        print(f"  [4.5] Copied: {src.name} → {ready_dir}")

    log_run(product_name, "file_organizer", "success", f"{len(jpegs)} files staged in {ready_dir}")
    print(f"  [4.5] Ready to upload: {ready_dir}")
    return ready_dir


def archive(product_name: str) -> None:
    """
    Moves files from ReadyToUpload/ to Archived/ after successful publish.
    Called by the logging phase (Phase 7.3).
    """
    src_dir  = READY_DIR / product_name
    arch_dir = _ROOT / "04_Assets" / "Archived" / product_name
    arch_dir.parent.mkdir(parents=True, exist_ok=True)

    if src_dir.exists():
        shutil.move(str(src_dir), str(arch_dir))
        print(f"  [7.3] Archived: {product_name} → {arch_dir}")
        log_run(product_name, "file_organizer", "success", f"Archived to {arch_dir}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/file_organizer.py <product_name> [--archive]")
        sys.exit(1)

    product = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else ""

    try:
        if mode == "--archive":
            archive(product)
        else:
            organize(product)
    except (FileNotFoundError, ValueError) as e:
        print(f"[error] {e}")
        sys.exit(1)
