#!/usr/bin/env bash
# =============================================================================
# run_monolith.sh — Bare-metal all-in-one launcher (no Docker required)
#
# Starts all four services as plain OS processes on one machine:
#   - Predictor   (port 18910) — dashboard + REST + WebSocket, always on
#   - Executor    (port 18911) — DRY_RUN by default, token-gated /execute
#   - Forge       (port 18912) — strategy lab, paper-trades against Predictor
#   - Signal Agent (no port)   — LLM enrichment poller, disabled by default
#
# This exists because each service has its own working-directory
# requirements that only line up automatically inside Docker (WORKDIR /app):
#   - predictor_server.py loads src/config.json (synced from the root
#     config.json below) and resolves model/data paths relative to CWD —
#     must run with CWD at repo root.
#   - forge must run as `python -m forge.server`, also from repo root
#     (forge/db.py's FORGE_DATA_DIR default is likewise repo-root-relative).
#   - executor/server.py has no special CWD requirement.
#   - signal_agent must run as `python -m signal_agent.main` from src/, so
#     the signal_agent package resolves correctly (there's an unrelated
#     signal_agent/Dockerfile at repo root that would otherwise shadow it).
#
# Usage:
#   bash run_monolith.sh [--no-executor] [--no-forge] [--no-agent] [--ollama]
#
# All flags are additive skips — by default all four services start.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"

NO_EXECUTOR=0
NO_FORGE=0
NO_AGENT=0
USE_OLLAMA=0
for arg in "$@"; do
    case "$arg" in
        --no-executor) NO_EXECUTOR=1 ;;
        --no-forge)    NO_FORGE=1 ;;
        --no-agent)    NO_AGENT=1 ;;
        --ollama)      USE_OLLAMA=1 ;;
    esac
done

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" &>/dev/null; then PYTHON="$candidate"; break; fi
    done
fi
[[ -z "$PYTHON" ]] && { echo "ERROR: Python not found."; exit 1; }
echo "[monolith] Using Python: $($PYTHON --version)"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    echo "[monolith] Loading .env…"
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

if [[ $USE_OLLAMA -eq 1 ]]; then
    export SA_INFERENCE_BACKEND=ollama
    export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
    export OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.1}"
fi

# Predictor's own WebSocket, for Forge to consume — bare-metal has no
# Docker DNS, so this must be localhost, not the container hostname
# "predictor" that docker-compose.yml uses.
export PREDICTOR_WS_URL="${PREDICTOR_WS_URL:-ws://localhost:18910/ws}"
export PREDICTOR_URL="${PREDICTOR_URL:-http://127.0.0.1:18910}"

PIDS=()
cleanup() {
    echo ""
    echo "[monolith] Shutting down…"
    for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
    wait 2>/dev/null || true
    echo "[monolith] Done."
}
trap cleanup INT TERM EXIT

# ── Sync config.json into src/ (see run_local.sh for why) ───────────────────
echo "[monolith] Syncing config.json -> src/config.json …"
cp "$SCRIPT_DIR/config.json" "$SRC_DIR/config.json"

# ── Predictor (CWD = repo root) ──────────────────────────────────────────────
echo "[monolith] Starting Predictor on :18910 …"
cd "$SCRIPT_DIR"
"$PYTHON" "$SRC_DIR/predictor_server.py" &
PIDS+=($!)
sleep 4

# ── Executor (CWD = repo root, DRY_RUN unless LIVE_CONFIRM is set) ─────────
if [[ $NO_EXECUTOR -eq 0 ]]; then
    echo "[monolith] Starting Executor on :18911 (dry_run=${DRY_RUN:-true}) …"
    "$PYTHON" "$SCRIPT_DIR/executor/server.py" &
    PIDS+=($!)
else
    echo "[monolith] Executor skipped (--no-executor)."
fi

# ── Forge (must run as -m forge.server, CWD = repo root) ────────────────────
if [[ $NO_FORGE -eq 0 ]]; then
    echo "[monolith] Starting Forge on :18912 …"
    (cd "$SCRIPT_DIR" && exec "$PYTHON" -m forge.server) &
    PIDS+=($!)
else
    echo "[monolith] Forge skipped (--no-forge)."
fi

# ── Signal Agent (must run as -m signal_agent.main, CWD = src/) ────────────
if [[ $NO_AGENT -eq 0 ]]; then
    echo "[monolith] Starting Signal Agent (backend=${SA_INFERENCE_BACKEND:-disabled}) …"
    (cd "$SRC_DIR" && exec "$PYTHON" -m signal_agent.main) &
    PIDS+=($!)
else
    echo "[monolith] Signal Agent skipped (--no-agent)."
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo " Antigravity Predictor — bare-metal monolith running"
echo ""
echo " Dashboard:       http://localhost:18910"
echo " Predictor status: http://localhost:18910/api/status"
echo " Feature parity:   http://localhost:18910/api/feature-parity/BTC_USDT"
[[ $NO_EXECUTOR -eq 0 ]] && echo " Executor health:  http://localhost:18911/health"
[[ $NO_FORGE    -eq 0 ]] && echo " Forge health:     http://localhost:18912/health"
echo ""
echo " Press Ctrl+C to stop all processes."
echo "══════════════════════════════════════════════════════"
echo ""

wait "${PIDS[@]}"
