#!/usr/bin/env bash
# loop.sh — Autonomous pipeline loop for VPS
#
# Two operating modes per cycle:
#
#   BUILD mode   — under weekly publish limit + cooldown expired
#                  Runs: Phase 1 (7-day) → Phase 2 (7-day) → Phase 3 (design next product)
#                        → Phase 4 → Phase 5 (publish) → Phase 6 → Phase 7
#                  Sleeps: SLEEP seconds (default 3600)
#
#   DESIGN mode  — under weekly limit but cooldown still active
#                  Runs: Phase 1 (7-day) → Phase 2 (7-day) → Phase 3 (design next product)
#                  Skips: Phase 4 / 5 / 6  (too soon to publish again)
#                  Sleeps: SLEEP seconds
#
#   MAINTAIN mode — weekly publish limit reached
#                  Runs: Phase 6 (SEO on all published listings) → Phase 7
#                  Sleeps: until Monday 00:00 UTC (weekly reset)
#
# Phases 1 & 2 each run at most once every 7 days (content & brand refresh cadence).
# Phase 3 runs every cycle in build/design mode — designs one new product per hour.
# Phase 5 is gated by WEEKLY_PUBLISH_LIMIT (default 5) and PUBLISH_COOLDOWN_HOURS (default 24).
# Template delivery to buyers (order_delivery.py) runs every cycle in every mode.
# A product that fails Phase 3B–5 is retried with lower priority and marked
# failed after 3 attempts, so it never blocks the rest of the queue.
#
# Configure in .env:
#   WEEKLY_PUBLISH_LIMIT=5        max listings per Mon–Sun week
#   PUBLISH_COOLDOWN_HOURS=24     min hours between consecutive publishes
#
# Usage:
#   ./loop.sh                    — run forever
#   ./loop.sh --once             — run one cycle then exit
#   SLEEP=7200 ./loop.sh         — override sleep interval
#
# Logs:
#   logs/loop.log                — all stdout/stderr
#   logs/loop_errors.log         — failures only
#
# Stop: kill $(cat logs/loop.pid)  or Ctrl+C

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="$SCRIPT_DIR/.venv/bin/python"

# Exit code for configuration errors. The systemd unit does not restart on it,
# so a broken credential stops the service instead of retrying every 30s.
EX_CONFIG=78
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/loop.log"
ERR_FILE="$LOG_DIR/loop_errors.log"
PID_FILE="$LOG_DIR/loop.pid"
SLEEP="${SLEEP:-3600}"
# Buyers wait at most this long for their template email while the loop sleeps
DELIVERY_INTERVAL="${DELIVERY_INTERVAL:-600}"
RUN_ONCE="${1:-}"

# ── Guards ────────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

if [[ ! -x "$PYTHON" ]]; then
    echo "[loop] ERROR: .venv not found. Run: ./setup_vps.sh" >&2
    exit "$EX_CONFIG"
fi

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "[loop] ERROR: .env not found. Copy .env.example and fill in API keys." >&2
    exit "$EX_CONFIG"
fi

# Read a value from .env with python-dotenv. .env is never sourced as shell:
# values with spaces (BRAND_NAME, Gmail app passwords) break `source`, and a
# value containing $(...) would run as a command.
_env_get() {
    "$PYTHON" -c 'import sys
from dotenv import dotenv_values
print(dotenv_values(".env").get(sys.argv[1]) or sys.argv[2])' "$1" "$2"
}

# Publishing pacing — configurable via .env (environment variables win)
WEEKLY_LIMIT="${WEEKLY_PUBLISH_LIMIT:-$(_env_get WEEKLY_PUBLISH_LIMIT 5)}"
COOLDOWN_HOURS="${PUBLISH_COOLDOWN_HOURS:-$(_env_get PUBLISH_COOLDOWN_HOURS 24)}"
[[ "$WEEKLY_LIMIT" =~ ^[0-9]+$ ]] || { echo "[loop] ERROR: WEEKLY_PUBLISH_LIMIT must be a whole number" >&2; exit "$EX_CONFIG"; }
[[ "$COOLDOWN_HOURS" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "[loop] ERROR: PUBLISH_COOLDOWN_HOURS must be a number" >&2; exit "$EX_CONFIG"; }

echo $$ > "$PID_FILE"

# ── Pre-flight credential check ───────────────────────────────────────────────
# Validates Stripe, Canva, and Anthropic credentials before doing any real work.
# Exits the loop immediately if Stripe credentials are broken (non-recoverable).
PREFLIGHT_CODE=0
"$PYTHON" scripts/preflight.py || PREFLIGHT_CODE=$?
if [[ $PREFLIGHT_CODE -eq 75 ]]; then
    echo "[loop] Pre-flight could not reach the APIs (network) — exiting so systemd retries later." >&2
    exit 75
fi
if [[ $PREFLIGHT_CODE -ne 0 ]]; then
    echo "[loop] Pre-flight failed — fix credentials and restart. Exiting." >&2
    "$PYTHON" scripts/alerts.py send "Pre-flight failed — loop stopped" \
        "Fix the credentials reported in logs/loop.log, then: systemctl restart commerce-pipeline"
    exit "$EX_CONFIG"
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
_ts()  { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

_log() {
    local msg="[loop] $(_ts) $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

_err() {
    local msg="[loop][ERROR] $(_ts) $*"
    echo "$msg" >&2
    echo "$msg" >> "$LOG_FILE"
    echo "$msg" >> "$ERR_FILE"
}

# Sleep for $1 seconds, waking every DELIVERY_INTERVAL to deliver new orders
_sleep_with_delivery() {
    local remaining="$1" chunk
    while (( remaining > 0 )); do
        chunk=$(( remaining < DELIVERY_INTERVAL ? remaining : DELIVERY_INTERVAL ))
        sleep "$chunk"
        remaining=$(( remaining - chunk ))
        if (( remaining > 0 )); then
            _run_phase "Order delivery" "$PYTHON" scripts/order_delivery.py \
                || _err "Order delivery had failures — will retry."
        fi
    done
}

_sleep_or_exit() {
    if [[ "$RUN_ONCE" == "--once" ]]; then
        _log "Run-once mode — exiting."
        exit 0
    fi
    _log "Sleeping ${SLEEP}s until next cycle (delivering orders every ${DELIVERY_INTERVAL}s)..."
    _sleep_with_delivery "$SLEEP"
}

_run_phase() {
    local label="$1"
    shift
    _log "Starting $label: $*"
    local code=0
    "$@" >> "$LOG_FILE" 2>&1 || code=$?
    if [[ $code -eq 0 ]]; then
        _log "$label completed OK"
        "$PYTHON" scripts/alerts.py ok "$label" >> "$LOG_FILE" 2>&1 || true
        return 0
    fi
    _err "$label FAILED (exit $code)"
    "$PYTHON" scripts/alerts.py fail "$label" "Exit $code. Last log lines:
$(tail -n 20 "$LOG_FILE")" >> "$LOG_FILE" 2>&1 || true
    return $code
}

# Count a failed publish attempt; after 3 the product is marked failed
_queue_failure() {
    local product="$1" reason="$2"
    local status
    status=$("$PYTHON" - "$product" "$reason" 2>>"$LOG_FILE" <<'PYEOF'
import sys; sys.path.insert(0, 'scripts')
from db import record_queue_failure
print(record_queue_failure(sys.argv[1], sys.argv[2]))
PYEOF
)
    if [[ "$status" == "failed" ]]; then
        _err "Product '$product' failed 3 times — marked failed and skipped. See run_log for details."
        "$PYTHON" scripts/alerts.py send "Product skipped: $product" \
            "It failed 3 publish attempts ($reason). Fix the cause, then set its queue row back to pending." \
            >> "$LOG_FILE" 2>&1 || true
    fi
}

# ── Trap clean exit ───────────────────────────────────────────────────────────
_cleanup() {
    _log "Loop stopped (signal received)."
    rm -f "$PID_FILE"
}
trap _cleanup INT TERM EXIT

# ── Helpers — file age check ──────────────────────────────────────────────────
_file_age_secs() {
    local f="$1"
    [[ -f "$f" ]] || { echo 999999; return; }
    local mod
    mod=$(date -r "$f" +%s 2>/dev/null || stat -c %Y "$f" 2>/dev/null || echo 0)
    echo $(( $(date +%s) - mod ))
}

# ── Main loop ─────────────────────────────────────────────────────────────────
_log "========================================"
_log "Autonomous loop starting. PID=$$"
_log "Weekly publish limit : ${WEEKLY_LIMIT} listings/week"
_log "Publish cooldown     : ${COOLDOWN_HOURS}h between listings"
_log "Sleep interval       : ${SLEEP}s"
_log "Logs: $LOG_FILE"
_log "========================================"

CYCLE=0
LAST_DIGEST_DATE=""
WEEK_LIMIT_SECS=$((7 * 24 * 3600))

while true; do
    CYCLE=$((CYCLE + 1))
    _log "──────── CYCLE $CYCLE START ────────"

    # ── Daily tasks (run once per calendar day) ───────────────────────────────
    TODAY=$(date -u '+%Y-%m-%d')
    if [[ "$TODAY" != "$LAST_DIGEST_DATE" ]]; then
        _run_phase "Sales sync"   "$PYTHON" scripts/sales_tracker.py \
            || _err "Sales sync failed — non-fatal."
        _run_phase "Email digest" "$PYTHON" scripts/email_digest.py \
            || _err "Email digest failed — non-fatal."
        _run_phase "Database backup" "$PYTHON" scripts/backup_db.py \
            || _err "Database backup failed — non-fatal."
        LAST_DIGEST_DATE="$TODAY"
    fi

    # ── Every cycle: send template links to buyers of new paid orders ─────────
    _run_phase "Order delivery" "$PYTHON" scripts/order_delivery.py \
        || _err "Order delivery had failures — will retry next cycle."

    # ── Check weekly publish budget ───────────────────────────────────────────
    PUBLISHED_THIS_WEEK=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYEOF'
import sys; sys.path.insert(0, 'scripts')
from db import count_published_this_week
print(count_published_this_week())
PYEOF
)
    if [[ ! "$PUBLISHED_THIS_WEEK" =~ ^[0-9]+$ ]]; then
        _err "Could not read the weekly publish count from the database — skipping this cycle."
        _sleep_or_exit
        continue
    fi
    _log "Weekly budget: ${PUBLISHED_THIS_WEEK}/${WEEKLY_LIMIT} listings published this week"

    # ── MAINTAIN MODE — weekly limit reached ─────────────────────────────────
    if [[ "$PUBLISHED_THIS_WEEK" -ge "$WEEKLY_LIMIT" ]]; then
        _log "━━━ MAINTAIN MODE — limit reached ($PUBLISHED_THIS_WEEK/$WEEKLY_LIMIT) ━━━"
        _log "Running SEO analysis on published listings..."

        # SEO analysis + auto-apply on published listings
        # Processes up to 10 listings per maintain cycle (oldest-SEO-reviewed first)
        # to keep each cycle short while still improving the full catalogue over time.
        "$PYTHON" - 2>>"$LOG_FILE" <<'PYEOF' || _err "Maintain SEO scan failed — non-fatal."
import sys, subprocess
sys.path.insert(0, 'scripts')
from db import get_conn

# Prioritise listings whose SEO was reviewed longest ago (or never reviewed)
with get_conn() as c:
    rows = c.execute("""
        SELECT l.product_name
        FROM listings l
        LEFT JOIN (
            SELECT product_name, MAX(reviewed_at) AS last_reviewed
            FROM seo_review GROUP BY product_name
        ) s ON l.product_name = s.product_name
        ORDER BY COALESCE(s.last_reviewed, '1970-01-01') ASC
        LIMIT 10
    """).fetchall()

for r in rows:
    try:
        result = subprocess.run(
            [sys.executable, 'scripts/seo_analyzer.py', r['product_name']],
            capture_output=True, text=True, timeout=120
        )
        if result.stdout: print(result.stdout.strip())
        if result.returncode != 0 and result.stderr:
            print(f"  [maintain] SEO error for {r['product_name']}: {result.stderr[:200]}")
    except Exception as e:
        print(f"  [maintain] SEO scan failed for {r['product_name']}: {e}")
PYEOF

        _run_phase "Storefront deploy" "$PYTHON" scripts/storefront.py deploy \
            || _err "Storefront deploy failed — non-fatal."

        _run_phase "Phase 7 (health dashboard)" "$PYTHON" scripts/health_dashboard.py \
            || _err "Phase 7 failed — non-fatal."

        # Sleep until Monday 00:00 UTC (weekly counter resets)
        SECS_TO_RESET=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYEOF'
import sys; sys.path.insert(0, 'scripts')
from db import seconds_until_week_reset
print(seconds_until_week_reset())
PYEOF
)
        HOURS_TO_RESET=$(( SECS_TO_RESET / 3600 ))
        _log "Weekly limit reached — maintain mode active. Next build cycle in ~${HOURS_TO_RESET}h (Monday reset)."
        _log "──────── CYCLE $CYCLE COMPLETE — maintain mode ────────"

        if [[ "$RUN_ONCE" == "--once" ]]; then
            _log "Run-once mode — exiting."
            exit 0
        fi
        _sleep_with_delivery "$SECS_TO_RESET"
        continue
    fi

    # ── BUILD / DESIGN MODE ───────────────────────────────────────────────────
    # Phase 1: Brand Builder — runs once every 7 days
    BRAND_AGE=$(_file_age_secs "$SCRIPT_DIR/outputs/brand_guide.md")
    if [[ "$BRAND_AGE" -gt "$WEEK_LIMIT_SECS" ]]; then
        _run_phase "Phase 1 (brand builder)" "$PYTHON" scripts/brand_crew.py \
            || { _log "Cycle $CYCLE aborted at phase 1."; _sleep_or_exit; continue; }
    else
        _log "Phase 1 skipped — brand_guide.md is $((BRAND_AGE / 3600))h old (refreshes after 168h)"
    fi

    # Phase 2: Listing content — runs once every 7 days (same cadence as brand)
    MASTER_AGE=$(_file_age_secs "$SCRIPT_DIR/outputs/master.txt")
    if [[ "$MASTER_AGE" -gt "$WEEK_LIMIT_SECS" ]]; then
        _run_phase "Phase 2 (listing generator)" "$PYTHON" scripts/listing_generator.py \
            || { _log "Cycle $CYCLE aborted at phase 2."; _sleep_or_exit; continue; }
    else
        _log "Phase 2 skipped — master.txt is $((MASTER_AGE / 3600))h old (refreshes after 168h)"
    fi

    # Phase 3: Canva image generation — picks next undesigned product from master.txt
    DESIGN_PRODUCT=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYEOF'
import sys, re
from pathlib import Path
sys.path.insert(0, 'scripts')
outputs = Path('outputs')
master  = outputs / 'master.txt'
if not master.exists():
    sys.exit(0)
text  = master.read_text(encoding='utf-8')
names = re.findall(r'^\d+\.\s+(.+?)\s+\(\$[\d.]+\)', text, re.MULTILINE)
for name in names:
    # Names come from model output and become folder names — never allow a path
    if '/' in name or '\\' in name or '..' in name:
        continue
    if not (outputs / f'{name.replace(" ", "_")}_canva.json').exists():
        print(name)
        break
PYEOF
)

    if [[ -n "$DESIGN_PRODUCT" ]]; then
        _log "Phase 3 — designing mockups: $DESIGN_PRODUCT"
        _run_phase "Phase 3 (canva image generator)" \
            "$PYTHON" scripts/canva_image_generator.py "$DESIGN_PRODUCT" \
            || _err "Phase 3 failed for: $DESIGN_PRODUCT — non-fatal, will retry next cycle."

        # Phase 3B: create the actual digital product (template) for the same product
        # Runs immediately after mockups — only if Phase 3 succeeded (product record exists)
        PRODUCT_RECORD="$SCRIPT_DIR/outputs/${DESIGN_PRODUCT// /_}_product.json"
        if [[ ! -f "$PRODUCT_RECORD" ]]; then
            _log "Phase 3B — creating digital product template: $DESIGN_PRODUCT"
            _run_phase "Phase 3B (canva product creator)" \
                "$PYTHON" scripts/canva_product_creator.py "$DESIGN_PRODUCT" \
                || _err "Phase 3B failed for: $DESIGN_PRODUCT — will retry before publishing; it will not publish without a template link."
        else
            _log "Phase 3B skipped — product template already exists for: $DESIGN_PRODUCT"
        fi
    else
        _log "Phase 3/3B — all products in master.txt have designs. Waiting for next Phase 2 refresh."
    fi

    # ── Check publish cooldown ────────────────────────────────────────────────
    COOLDOWN_OK=$("$PYTHON" - "$COOLDOWN_HOURS" 2>>"$LOG_FILE" <<'PYEOF'
import sys; sys.path.insert(0, 'scripts')
from db import hours_since_last_publish
cooldown = float(sys.argv[1])
elapsed  = hours_since_last_publish()
print('yes' if elapsed >= cooldown else f'no:{elapsed:.1f}')
PYEOF
)

    if [[ "$COOLDOWN_OK" != "yes" ]]; then
        ELAPSED="${COOLDOWN_OK#no:}"
        WAIT_H=$(echo "$COOLDOWN_HOURS $ELAPSED" | awk '{printf "%.1f", $1 - $2}')
        _log "━━━ DESIGN MODE — cooldown active (${ELAPSED}h elapsed, need ${COOLDOWN_HOURS}h) ━━━"
        _log "Designs are being generated. Publishing resumes in ~${WAIT_H}h."
        _log "──────── CYCLE $CYCLE COMPLETE — design mode ────────"
        _sleep_or_exit
        continue
    fi

    # ── BUILD MODE — cooldown cleared, publish next pending product ───────────
    PRODUCT=$("$PYTHON" - 2>>"$LOG_FILE" <<'PYEOF'
import sys; sys.path.insert(0, 'scripts')
from db import next_publishable_product
print(next_publishable_product())
PYEOF
)

    if [[ -z "$PRODUCT" ]]; then
        _log "No pending products ready to publish this cycle."
        _sleep_or_exit
        continue
    fi

    _log "━━━ BUILD MODE — publishing: $PRODUCT ━━━"

    # Phase 3B retry: a product needs a template link before it can be sold
    if [[ ! -f "$SCRIPT_DIR/outputs/${PRODUCT// /_}_product.json" ]]; then
        _run_phase "Phase 3B retry (canva product creator)" \
            "$PYTHON" scripts/canva_product_creator.py "$PRODUCT" \
            || { _queue_failure "$PRODUCT" "phase 3B: no template link"; _log "Cycle $CYCLE aborted at phase 3B."; _sleep_or_exit; continue; }
    fi

    # Phase 4: Process + Stage
    _run_phase "Phase 4.1 (image processor)"  "$PYTHON" scripts/image_processor.py      "$PRODUCT" \
        || { _queue_failure "$PRODUCT" "phase 4.1"; _log "Cycle $CYCLE aborted at phase 4.1."; _sleep_or_exit; continue; }

    _run_phase "Phase 4.2 (listing builder)"  "$PYTHON" scripts/listing_builder.py      "$PRODUCT" \
        || { _queue_failure "$PRODUCT" "phase 4.2"; _log "Cycle $CYCLE aborted at phase 4.2."; _sleep_or_exit; continue; }

    _run_phase "Phase 4.3 (validator)"        "$PYTHON" scripts/pre_upload_validator.py  "$PRODUCT" \
        || { _queue_failure "$PRODUCT" "phase 4.3 validation"; _log "Cycle $CYCLE aborted at phase 4.3 — validation failed."; _sleep_or_exit; continue; }

    _run_phase "Phase 4.5 (file organizer)"   "$PYTHON" scripts/file_organizer.py       "$PRODUCT" \
        || { _queue_failure "$PRODUCT" "phase 4.5"; _log "Cycle $CYCLE aborted at phase 4.5."; _sleep_or_exit; continue; }

    # Phase 5: Create the Stripe product (draft unless STORE_PUBLISH_STATUS=active)
    _run_phase "Phase 5 (stripe publisher)"   "$PYTHON" scripts/stripe_publisher.py     "$PRODUCT" \
        || { _queue_failure "$PRODUCT" "phase 5 upload"; _log "Cycle $CYCLE aborted at phase 5."; _sleep_or_exit; continue; }

    # Phase 6: SEO analysis on the new listing (non-fatal)
    _run_phase "Phase 6 (seo analyzer)"       "$PYTHON" scripts/seo_analyzer.py         "$PRODUCT" \
        || _err "Phase 6 failed — listing is saved, SEO skipped."

    # Rebuild and deploy the storefront (shows approved products only)
    _run_phase "Storefront deploy" "$PYTHON" scripts/storefront.py deploy \
        || _err "Storefront deploy failed — non-fatal."

    # Phase 7: Health dashboard
    _run_phase "Phase 7 (health dashboard)"   "$PYTHON" scripts/health_dashboard.py \
        || _err "Phase 7 failed — non-fatal."

    _log "──────── CYCLE $CYCLE COMPLETE — published: $PRODUCT ($((PUBLISHED_THIS_WEEK + 1))/${WEEKLY_LIMIT} this week) ────────"

    _sleep_or_exit
done
