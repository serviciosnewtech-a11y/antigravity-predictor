#!/usr/bin/env bash
# =============================================================================
# run_local.sh — Local smoke test launcher for the Antigravity Predictor
#
# Starts the Predictor server + Hermes signal agent in one terminal.
# Both processes log to stdout, Ctrl+C stops both cleanly.
#
# Usage:
#   cd /path/to/Predictor
#   bash run_local.sh [--ollama] [--no-agent]
#
# Options:
#   --ollama     Use local Ollama for signal synthesis (no API key needed)
#   --no-agent   Skip the signal agent — run predictor only
#
# Requirements:
#   Python 3.10+, pip packages (see below)
#   If using Claude: set ANTHROPIC_API_KEY in your shell or create a .env file
#   If using Ollama: ollama serve + ollama pull llama3.1 (or set OLLAMA_MODEL)
#
# First run (installs deps):
#   pip install fastapi uvicorn websockets loguru requests lightgbm \
#               pandas numpy scikit-learn pyarrow yfinance ccxt \
#               anthropic duckduckgo-search
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"

# ── Flags ──────────────────────────────────────────────────────────────────
USE_OLLAMA=0
NO_AGENT=0
for arg in "$@"; do
    case "$arg" in
        --ollama)   USE_OLLAMA=1 ;;
        --no-agent) NO_AGENT=1 ;;
    esac
done

# ── Python detection ───────────────────────────────────────────────────────
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" &>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    done
fi
[[ -z "$PYTHON" ]] && { echo "ERROR: Python not found."; exit 1; }
echo "[run_local] Using Python: $($PYTHON --version)"

# ── Load .env if present ───────────────────────────────────────────────────
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    echo "[run_local] Loading .env…"
    set -a
    # shellcheck disable=SC1090
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Inference backend ──────────────────────────────────────────────────────
if [[ $USE_OLLAMA -eq 1 ]]; then
    export SA_INFERENCE_BACKEND=ollama
    export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
    export OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.1}"
    echo "[run_local] Signal agent will use Ollama ($OLLAMA_URL, model=$OLLAMA_MODEL)"
else
    export SA_INFERENCE_BACKEND="${SA_INFERENCE_BACKEND:-claude}"
    if [[ "$SA_INFERENCE_BACKEND" == "claude" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
        echo ""
        echo "  WARNING: ANTHROPIC_API_KEY is not set."
        echo "  Signal agent enrichment will log an error but won't crash."
        echo "  To fix: export ANTHROPIC_API_KEY=sk-ant-..."
        echo "  Or use --ollama for local inference."
        echo ""
    fi
fi

# ── Cleanup on exit ────────────────────────────────────────────────────────
PIDS=()
cleanup() {
    echo ""
    echo "[run_local] Shutting down…"
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "[run_local] Done."
}
trap cleanup INT TERM EXIT

# ── Sync config.json into src/ ─────────────────────────────────────────────
# predictor_server.py always loads its config from src/config.json
# (resolved relative to the script's own location, not CWD — see
# CONFIG_PATH in predictor_server.py). The Docker build produces this via
# `COPY config.json src/config.json`; local runs need the same sync, done
# fresh every run so a stale/machine-specific src/config.json can never
# silently drift from the canonical root config.json.
echo "[run_local] Syncing config.json -> src/config.json …"
cp "$SCRIPT_DIR/config.json" "$SRC_DIR/config.json"

# ── Start Predictor server ─────────────────────────────────────────────────
# Run with CWD at the repo root (not src/) so the relative model paths in
# config.json ("models/model_btc_long.txt") resolve the same way they do
# in Docker (WORKDIR /app, CMD runs `python src/predictor_server.py` from
# /app). cd-ing into src/ first would break that resolution.
echo "[run_local] Starting Predictor server on http://localhost:18910 …"
cd "$SCRIPT_DIR"
"$PYTHON" "$SRC_DIR/predictor_server.py" &
PIDS+=($!)
echo "[run_local] Predictor PID: ${PIDS[-1]}"

# Give predictor a moment to start (model loading can take a few seconds)
sleep 4

# ── Start Signal Agent ─────────────────────────────────────────────────────
# Needs CWD=src/ so `signal_agent` resolves as a top-level importable
# package (it lives at src/signal_agent/, and there's an unrelated
# src-level signal_agent/Dockerfile at repo root that would otherwise
# shadow it). Run in a subshell so this cd doesn't affect the rest of the
# script — the predictor above needs CWD at the repo root instead.
if [[ $NO_AGENT -eq 0 ]]; then
    echo "[run_local] Starting Hermes signal agent (backend=$SA_INFERENCE_BACKEND)…"
    (cd "$SRC_DIR" && exec "$PYTHON" -m signal_agent.main) &
    PIDS+=($!)
    echo "[run_local] Signal agent PID: ${PIDS[-1]}"
else
    echo "[run_local] Signal agent skipped (--no-agent)."
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo " Antigravity Predictor — local smoke test running"
echo ""
echo " Dashboard:  http://localhost:18910"
echo " API status: http://localhost:18910/api/status"
echo " Enriched:   http://localhost:18910/api/enriched-signals"
echo ""
echo " Press Ctrl+C to stop all processes."
echo "══════════════════════════════════════════════════════"
echo ""

# Wait for both child processes
wait "${PIDS[@]}"
