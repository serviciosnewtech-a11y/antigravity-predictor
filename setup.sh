#!/usr/bin/env bash
# =============================================================================
# setup.sh — One-time setup for the Antigravity Predictor (local deployment)
#
# Creates a Python virtual environment and installs all dependencies.
# Run this once after cloning/copying the folder to a new machine.
#
# Usage:
#   bash setup.sh
#
# After setup:
#   cp .env.example .env        # fill in OLLAMA_URL (or ANTHROPIC_API_KEY)
#   bash run_local.sh --ollama  # start everything
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ── Python detection ───────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        VER=$("$candidate" -c "import sys; print(sys.version_info[:2])")
        if "$candidate" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.10+ not found. Install it first."
    echo "  Ubuntu/Debian: sudo apt install python3.11"
    echo "  macOS:         brew install python@3.11"
    exit 1
fi
echo "[setup] Using $("$PYTHON" --version)"

# ── Create virtual environment ─────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    echo "[setup] Creating virtual environment at .venv …"
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "[setup] Virtual environment already exists — skipping creation."
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# ── Upgrade pip ────────────────────────────────────────────────────────────
echo "[setup] Upgrading pip …"
"$VENV_PIP" install --upgrade pip --quiet

# ── Install dependencies ───────────────────────────────────────────────────
echo "[setup] Installing dependencies (this may take a minute) …"
"$VENV_PIP" install \
    fastapi \
    "uvicorn[standard]" \
    websockets \
    loguru \
    requests \
    lightgbm \
    pandas \
    numpy \
    scikit-learn \
    pyarrow \
    yfinance \
    ccxt \
    anthropic \
    "duckduckgo-search" \
    --quiet

echo "[setup] All dependencies installed."

# ── .env setup hint ────────────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo ""
    echo "  Next step: create your .env file"
    echo "  ──────────────────────────────────"
    echo "  cp .env.example .env"
    echo "  # then edit .env and set OLLAMA_URL to your Ollama machine's IP"
    echo ""
fi

# ── Patch run_local.sh to use venv ────────────────────────────────────────
# run_local.sh auto-detects the venv python if PYTHON env var is set
echo "[setup] Done. To start:"
echo ""
echo "  PYTHON=$VENV_PYTHON bash run_local.sh --ollama"
echo ""
echo "  Or just activate the venv first:"
echo "  source .venv/bin/activate"
echo "  bash run_local.sh --ollama"
echo ""
