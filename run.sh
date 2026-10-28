#!/usr/bin/env bash
# run.sh — VPS entry point for the Canva → Stripe storefront pipeline
#
# Usage:
#   ./run.sh db-init                    — initialise SQLite database (run once on new VPS)
#   ./run.sh sales [--full]             — sync Stripe sales & revenue to SQLite
#   ./run.sh phase1                     — Brand Builder crew (writes outputs/brand_guide.md)
#   ./run.sh phase2                     — Launch Executor (writes outputs/master.txt)
#   ./run.sh phase2-rich                — Autonomous Executor (richer prompt engine)
#   ./run.sh phase3 <product> [--price] — Canva image generator (6 mockups via Claude + Canva MCP)
#   ./run.sh phase3b <product> [--price]— Canva product creator (actual template buyers download)
#   ./run.sh phase4 <product>           — Process images → build listing → validate → stage
#   ./run.sh canva-auth                 — One-time Canva API OAuth setup (saves tokens to .env)
#   ./run.sh stripe-auth                — One-time Stripe key setup and check (saves key to .env)
#   ./run.sh phase5 <product>           — Create the Stripe product (draft until approved)
#   ./run.sh phase6 <product>           — SEO gap analysis + save best tags/meta for the storefront
#   ./run.sh phase7                     — Daily health dashboard
#   ./run.sh approve <product>          — Make a reviewed draft live (--list, --preview <product>)
#   ./run.sh deliver [--dry-run]        — Email template links to buyers of paid checkouts
#   ./run.sh store-build                — Build the static storefront into site/
#   ./run.sh store-deploy               — Build and push the storefront to its git host
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
        echo "[run.sh] Syncing sales & revenue from Stripe"
        "$PYTHON" scripts/sales_tracker.py ${2:+$2}
        ;;
    phase1)
        echo "[run.sh] Starting Phase 1 — Brand Builder"
        "$PYTHON" scripts/brand_crew.py
        ;;
    phase2)
        echo "[run.sh] Starting Phase 2 — Launch Executor"
        "$PYTHON" scripts/listing_generator.py
        ;;
    phase2-rich)
        echo "[run.sh] Starting Phase 2 (rich) — Autonomous Executor"
        "$PYTHON" scripts/listing_generator.py
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
    stripe-auth)
        echo "[run.sh] Checking Stripe setup"
        "$PYTHON" scripts/stripe_setup.py
        ;;
    phase5)
        _require_product
        echo "[run.sh] Phase 5 — Create Stripe product: $PRODUCT"
        "$PYTHON" scripts/stripe_publisher.py "$PRODUCT"
        ;;
    phase6)
        _require_product
        echo "[run.sh] Phase 6 — SEO analysis + storefront tags: $PRODUCT"
        "$PYTHON" scripts/seo_analyzer.py "$PRODUCT"
        "$PYTHON" scripts/storefront.py build
        ;;
    phase7)
        echo "[run.sh] Phase 7 — Health dashboard"
        "$PYTHON" scripts/health_dashboard.py
        ;;
    approve)
        echo "[run.sh] Approving draft product"
        "$PYTHON" scripts/approve_listing.py "${@:2}"
        ;;
    deliver)
        echo "[run.sh] Delivering template links to buyers"
        "$PYTHON" scripts/order_delivery.py ${2:+"$2"}
        ;;
    store-build)
        echo "[run.sh] Building storefront"
        "$PYTHON" scripts/storefront.py build
        ;;
    store-deploy)
        echo "[run.sh] Building and deploying storefront"
        "$PYTHON" scripts/storefront.py deploy
        ;;
    backup)
        echo "[run.sh] Backing up database"
        "$PYTHON" scripts/backup_db.py
        ;;
    all)
        echo "[run.sh] Running full pipeline: Phase 1 → Phase 2"
        "$PYTHON" scripts/brand_crew.py
        "$PYTHON" scripts/listing_generator.py
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
        "$PYTHON" scripts/stripe_publisher.py "$PRODUCT"
        "$PYTHON" scripts/seo_analyzer.py "$PRODUCT" || true
        "$PYTHON" scripts/storefront.py deploy || true
        ;;
    *)
        echo "Usage: $0 {db-init|preflight|digest|sales|canva-auth|stripe-auth|phase1|phase2|phase2-rich|phase3|phase3b|phase4|phase5|phase6|phase7|approve|deliver|store-build|store-deploy|backup|all|full|design} [product_name]"
        exit 1
        ;;
esac
