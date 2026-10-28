#!/usr/bin/env bash
# setup_vps.sh — Phase 0: one-time VPS environment bootstrap
#
# Run once after cloning the repo to a new server:
#   chmod +x setup_vps.sh && ./setup_vps.sh
#
# What it does:
#   1. Creates all required pipeline directories
#   2. Creates .env from .env.example if not present
#   3. Creates Python virtual environment
#   4. Installs pip dependencies
#   5. Initialises the SQLite database
#   6. Sets executable bits on run.sh / loop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  Multi-Agent Commerce Pipeline — VPS Setup"
echo "========================================"
echo ""

# 1 — Directory structure
echo "[1/6] Creating pipeline directories..."
mkdir -p \
    02_Products \
    03_Canva_Exports \
    "04_Assets/ReadyToUpload" \
    "04_Assets/Archived" \
    outputs \
    logs
echo "      Done."

# 2 — .env
echo "[2/6] Checking .env..."
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
        cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
        echo "      Created .env from .env.example"
        echo "      → Fill in all API keys before starting the pipeline."
    else
        echo "      WARNING: .env.example not found. Create .env manually."
    fi
else
    echo "      .env already exists — skipping."
fi

# 3 — Virtual environment
echo "[3/6] Setting up Python virtual environment..."
if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then
    python3.12 -m venv "$SCRIPT_DIR/.venv"
    echo "      Created .venv with python3.12"
else
    echo "      .venv already exists — skipping."
fi

PYTHON="$SCRIPT_DIR/.venv/bin/python"
PIP="$SCRIPT_DIR/.venv/bin/pip"

# 4 — pip dependencies
echo "[4/6] Installing pip dependencies..."
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -r requirements.txt
echo "      Done."

# 5 — Database
echo "[5/6] Initialising SQLite database..."
"$PYTHON" scripts/db.py
echo "      Done."

# 6 — Executable bits
echo "[6/6] Setting executable permissions..."
chmod +x "$SCRIPT_DIR/run.sh"
chmod +x "$SCRIPT_DIR/loop.sh"
chmod +x "$SCRIPT_DIR/setup_vps.sh"
echo "      Done."

echo ""
echo "========================================"
echo "  Setup complete."
echo ""
echo "  Next steps:"
echo ""
echo "  1. Fill in .env with your API keys:"
echo "     nano .env"
echo ""
echo "  2. One-time OAuth setup (run on your Mac, needs a browser):"
echo "     python scripts/stripe_setup.py  ← Stripe secret key (start with sk_test_)"
echo "     python scripts/canva_oauth.py   ← Canva API tokens"
echo ""
echo "  3. Run pre-flight to confirm everything is connected:"
echo "     ./run.sh check"
echo ""
echo "  4. Install the systemd service so the loop survives reboots:"
echo "     cp commerce-pipeline.service /etc/systemd/system/"
echo "     systemctl daemon-reload"
echo "     systemctl enable commerce-pipeline"
echo "     systemctl start commerce-pipeline"
echo ""
echo "  5. Check it's running:"
echo "     systemctl status commerce-pipeline"
echo "     tail -f logs/loop.log"
echo "========================================"
