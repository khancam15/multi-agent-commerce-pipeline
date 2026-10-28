#!/usr/bin/env bash
# run.sh — VPS entry point for the Canva → Shopify pipeline
#
# Usage:
#   ./run.sh db-init                    — initialise SQLite database (run once on new VPS)
#   ./run.sh sales [--full]             — sync Shopify orders & revenue to SQLite
#   ./run.sh phase1                     — Brand Builder crew (writes outputs/brand_guide.md)
#   ./run.sh phase2                     — Launch Executor (writes outputs/master.txt)
#   ./run.sh phase2-rich                — Autonomous Executor (richer prompt engine)
#   ./run.sh phase3 <product> [--price] — Canva image generator (6 mockups via Claude + Canva MCP)
#   ./run.sh phase3b <product> [--price]— Canva product creator (actual template buyers download)
#   ./run.sh phase4 <product>           — Process images → build listing → validate → stage
#   ./run.sh canva-auth                 — One-time Canva API OAuth setup (saves tokens to .env)
#   ./run.sh shopify-auth               — One-time Shopify Admin API setup (saves tokens to .env)
#   ./run.sh phase5 <product>           — Upload staged listing to Shopify via Admin API
#   ./run.sh phase6 <product>           — SEO gap analysis + auto-apply best tags to live listing
#   ./run.sh phase7                     — Daily health dashboard
#   ./run.sh approve <product>          — Make a reviewed draft product live (--list to see drafts)
#   ./run.sh deliver [--dry-run]        — Email template links to buyers of paid orders
#   ./run.sh backfill [--apply]         — Move template links out of old product descriptions
#   ./run.sh backup                     — Back up the SQLite database to backups/
#   ./run.sh all                        — Full pipeline: phase1 → phase2
#   ./run.sh design <product> [--price] — Alias for phase3
#   ./run.sh full <product>             — Full autonomous: phase3 → 3B → 4 → 5 → 6
#
# Cron examples:
#   0 6 * * * /path/to/project/run.sh phase2 >> /path/to/project/outputs/cron.log 2>&1
#   0 8 * * * /path/to/project/run.sh phase7 >> /path/to/project/outputs/cron.log 2>&1

set -euo pipefail

# Resolve the project root regardless of CWD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "[error] Virtual environment not found at .venv/"
    echo "        Run: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "[error] .env file not found — copy .env.example and fill in your API keys"
    exit 1
fi

PHASE="${1:-}"
PRODUCT="${2:-}"

# Phase 4–6 and full require a product name argument
_require_product() {
    if [[ -z "$PRODUCT" ]]; then
        echo "[error] Phase $PHASE requires a product name: ./run.sh $PHASE <product_name>"
        exit 1
    fi
}

case "$PHASE" in
    db-init)
        echo "[run.sh] Initialising database"
        "$PYTHON" scripts/db.py
        ;;
    preflight|check)
        echo "[run.sh] Running pre-flight credential check"
        "$PYTHON" scripts/preflight.py
        ;;
    digest)
        echo "[run.sh] Sending email digest now"
        "$PYTHON" scripts/email_digest.py
        ;;
    sales)
        echo "[run.sh] Syncing sales & revenue from Shopify"
        "$PYTHON" scripts/sales_tracker.py ${2:+$2}
        ;;
    phase1)
        echo "[run.sh] Starting Phase 1 — Brand Builder"
        "$PYTHON" scripts/shopify_brand_crew.py
        ;;
    phase2)
        echo "[run.sh] Starting Phase 2 — Launch Executor"
        "$PYTHON" scripts/shopify_autonomous.py
        ;;
    phase2-rich)
        echo "[run.sh] Starting Phase 2 (rich) — Autonomous Executor"
        "$PYTHON" scripts/shopify_autonomous.py
        ;;
    phase3|design)
        _require_product
        echo "[run.sh] Phase 3 — Canva Image Generator: $PRODUCT"
        "$PYTHON" scripts/canva_image_generator.py "$PRODUCT" ${3:+--price "$3"}
        ;;
    phase3b|product)
        _require_product
        echo "[run.sh] Phase 3B — Canva Product Creator: $PRODUCT"
        "$PYTHON" scripts/canva_product_creator.py "$PRODUCT" ${3:+--price "$3"}
        ;;
    phase4)
        _require_product
        echo "[run.sh] Phase 4 — Process images, build listing, validate, stage: $PRODUCT"
        "$PYTHON" scripts/image_processor.py "$PRODUCT"
        "$PYTHON" scripts/listing_builder.py "$PRODUCT"
        "$PYTHON" scripts/pre_upload_validator.py "$PRODUCT"
        "$PYTHON" scripts/file_organizer.py "$PRODUCT"
        ;;
    canva-auth)
        echo "[run.sh] Starting Canva API OAuth setup (💻 HOST only)"
        "$PYTHON" scripts/canva_oauth.py
        ;;
    shopify-auth)
        echo "[run.sh] Starting Shopify Admin API setup (💻 HOST only)"
        "$PYTHON" scripts/shopify_setup.py
        ;;
    phase5)
        _require_product
        echo "[run.sh] Phase 5 — Upload to Shopify via Admin API: $PRODUCT"
        "$PYTHON" scripts/shopify_uploader.py "$PRODUCT"
        ;;
    phase6)
        _require_product
        echo "[run.sh] Phase 6 — SEO analysis + Shopify tag update: $PRODUCT"
        "$PYTHON" scripts/seo_analyzer.py "$PRODUCT"
        ;;
    phase7)
        echo "[run.sh] Phase 7 — Health dashboard"
        "$PYTHON" scripts/health_dashboard.py
        ;;
    approve)
        echo "[run.sh] Approving draft product"
        "$PYTHON" scripts/approve_listing.py ${2:+"$2"}
        ;;
    deliver)
        echo "[run.sh] Delivering template links to buyers"
        "$PYTHON" scripts/order_delivery.py ${2:+"$2"}
        ;;
    backfill)
        echo "[run.sh] Backfilling template links for existing products"
        "$PYTHON" scripts/backfill_template_links.py ${2:+"$2"}
        ;;
    backup)
        echo "[run.sh] Backing up database"
        "$PYTHON" scripts/backup_db.py
        ;;
    all)
        echo "[run.sh] Running full pipeline: Phase 1 → Phase 2"
        "$PYTHON" scripts/shopify_brand_crew.py
        "$PYTHON" scripts/shopify_autonomous.py
        ;;
    full)
        _require_product
        echo "[run.sh] Full autonomous pipeline: Phase 3 → 3B → 4 → 5 → 6 for: $PRODUCT"
        "$PYTHON" scripts/canva_image_generator.py "$PRODUCT" ${3:+--price "$3"}
        "$PYTHON" scripts/canva_product_creator.py "$PRODUCT" ${3:+--price "$3"}
        "$PYTHON" scripts/image_processor.py "$PRODUCT"
        "$PYTHON" scripts/listing_builder.py "$PRODUCT"
        "$PYTHON" scripts/pre_upload_validator.py "$PRODUCT"
        "$PYTHON" scripts/file_organizer.py "$PRODUCT"
        "$PYTHON" scripts/shopify_uploader.py "$PRODUCT"
        "$PYTHON" scripts/seo_analyzer.py "$PRODUCT" || true
        ;;
    *)
        echo "Usage: $0 {db-init|preflight|digest|sales|canva-auth|shopify-auth|phase1|phase2|phase2-rich|phase3|phase3b|phase4|phase5|phase6|phase7|approve|deliver|backfill|backup|all|full|design} [product_name]"
        exit 1
        ;;
esac
