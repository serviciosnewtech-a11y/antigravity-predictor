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

if [[ ! -f "$SCRIPT_DIR/.env" && -f "$SCRIPT_DIR/.env.example" ]]; then
    echo "[monolith] No .env found — copying from .env.example…"
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
fi

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    echo "[monolith] Loading .env…"
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

# ── Auto-generate INTERNAL_API_TOKEN if it's empty ──────────────────────────
# INTERNAL_API_TOKEN gates the enriched-signal write endpoint (POST
# /api/enriched-signal/{asset}), used by the signal agent when enabled. Left
# blank in .env.example on purpose (no secret should ship pre-set), but a
# permanently-blank token means that endpoint 503s forever until an operator
# manually notices and sets one — deploy.sh (Docker path) already
# auto-generates one on first run; this mirrors that for bare metal so the
# two paths don't silently diverge in default security posture.
if [[ -z "${INTERNAL_API_TOKEN:-}" && -f "$SCRIPT_DIR/.env" ]]; then
    NEW_TOKEN="$($PYTHON -c 'import secrets; print(secrets.token_hex(32))')"
    if grep -q '^INTERNAL_API_TOKEN=' "$SCRIPT_DIR/.env"; then
        sed -i "s/^INTERNAL_API_TOKEN=.*/INTERNAL_API_TOKEN=${NEW_TOKEN}/" "$SCRIPT_DIR/.env"
    else
        echo "INTERNAL_API_TOKEN=${NEW_TOKEN}" >> "$SCRIPT_DIR/.env"
    fi
    export INTERNAL_API_TOKEN="$NEW_TOKEN"
    echo "[monolith] INTERNAL_API_TOKEN was empty — generated a new one and saved it to .env."
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

# ── Refresh macro data (gold/oil/dxy/spx/vix), best-effort ──────────────────
# data/macro/*.parquet ships pre-populated in the deployment package (small,
# daily OHLCV, committed to git despite data/ being otherwise gitignored —
# see .gitignore's comment). This refresh keeps it from going stale on a
# long-running deployment. It's genuinely best-effort: yfinance is a
# third-party scrape that can rate-limit or be briefly unreachable, and
# predictor_server.py's /api/market-tickers already degrades gracefully
# (rate-limited warning log, not a crash) if the file is missing or stale —
# so this must never block or fail the overall launch.
echo "[monolith] Refreshing macro data (gold/oil/dxy/spx/vix), best-effort…"
( cd "$SCRIPT_DIR" && timeout 20s "$PYTHON" src/fetch_macro.py --data-dir data/macro --days 730 \
    || echo "[monolith] Macro refresh skipped (offline or yfinance unavailable) — using existing data/macro/*.parquet if present." ) &

# ── Agent-agnostic chat relay (opt-in) — local CLI-agent backend for the ───
# chat surfaces, instead of a remote OpenAI-compatible API. Bridges
# predictor's HERMES_PROXY_URL calls to whatever local CLI agent
# AGENT_RELAY_CMD names (default: `hermes --profile metis chat -q {prompt}`
# — any CLI-based agent works, not just Hermes/Metis). Off unless
# ENABLE_AGENT_RELAY=true. If HERMES_PROXY_URL is unset, this also exports
# it pointing at the relay automatically — turning the flag on is enough,
# no separate URL config needed for the zero-config case.
if [[ "${ENABLE_AGENT_RELAY:-false}" == "true" ]]; then
    export AGENT_RELAY_PORT="${AGENT_RELAY_PORT:-8645}"

    # Kill anything already bound to this port before starting a new relay.
    # Without this, a stale relay process left over from an earlier
    # deployment/test run (different code, possibly an old buggy version)
    # can keep answering on this port while the new one silently fails to
    # bind — predictor then talks to the OLD process and nobody notices,
    # because startup here doesn't fail loudly either way. This has
    # actually happened: a fixed relay shipped correctly in a package but
    # a stale pre-fix process on the target machine kept serving requests.
    STALE_PID="$(ss -ltnp 2>/dev/null | grep -E ":${AGENT_RELAY_PORT}\b" | grep -oP 'pid=\K[0-9]+' | head -1)"
    if [[ -n "$STALE_PID" ]]; then
        echo "[monolith] Port ${AGENT_RELAY_PORT} already has a process on it (pid $STALE_PID) — killing it before starting the new relay."
        kill "$STALE_PID" 2>/dev/null || true
        sleep 1
    fi

    echo "[monolith] Starting agent chat relay on :${AGENT_RELAY_PORT} (cmd: ${AGENT_RELAY_CMD:-hermes --profile metis chat -q {prompt}}) …"
    "$PYTHON" "$SCRIPT_DIR/tools/agent_chat_relay.py" &
    PIDS+=($!)
    sleep 1

    # Verify it's actually OUR relay answering, not some other stale
    # process — the new /health response always has a "live_ok" key;
    # anything else means something's wrong (old code, or nothing there).
    RELAY_HEALTH="$(curl -fsS -m 3 "http://127.0.0.1:${AGENT_RELAY_PORT}/health" 2>/dev/null || echo "")"
    if [[ "$RELAY_HEALTH" != *'"live_ok"'* ]]; then
        echo "[monolith] WARNING: agent relay on :${AGENT_RELAY_PORT} did not respond with the expected health schema — it may not have started correctly, or something else is bound to that port. Response: ${RELAY_HEALTH:-<no response>}"
    else
        echo "[monolith] Agent relay confirmed responding on :${AGENT_RELAY_PORT}."
    fi

    if [[ -z "${HERMES_PROXY_URL:-}" ]]; then
        export HERMES_PROXY_URL="http://127.0.0.1:${AGENT_RELAY_PORT}"
        echo "[monolith] HERMES_PROXY_URL was unset — pointing it at the agent relay automatically."
    fi
else
    echo "[monolith] Agent chat relay skipped (ENABLE_AGENT_RELAY is not 'true')."
fi

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

# ── Admin Agent (opt-in only — UNRESTRICTED shell access, operator-only) ───
# Off unless BOTH ENABLE_ADMIN_AGENT=true AND ADMIN_API_TOKEN are set in
# .env — this is deliberate: the admin agent gives an LLM real shell access
# on this machine with no per-command confirmation, gated only by the
# bearer token. It is never started silently. Talk to it with
# `python3 tools/admin_chat.py`, never via the public dashboard.
if [[ "${ENABLE_ADMIN_AGENT:-false}" == "true" ]]; then
    if [[ -z "${ADMIN_API_TOKEN:-}" ]]; then
        echo "[monolith] ENABLE_ADMIN_AGENT=true but ADMIN_API_TOKEN is unset — refusing to start it unprotected. Set ADMIN_API_TOKEN in .env."
    else
        echo "[monolith] Starting Admin Agent on :${ADMIN_AGENT_PORT:-18913} — UNRESTRICTED shell access, operator-only (see admin_agent/server.py) …"
        "$PYTHON" "$SCRIPT_DIR/admin_agent/server.py" &
        PIDS+=($!)
    fi
else
    echo "[monolith] Admin Agent skipped (ENABLE_ADMIN_AGENT is not 'true')."
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
[[ "${ENABLE_ADMIN_AGENT:-false}" == "true" && -n "${ADMIN_API_TOKEN:-}" ]] && echo " Admin agent:      http://localhost:${ADMIN_AGENT_PORT:-18913}/health  (talk to it via: python3 tools/admin_chat.py)"
[[ "${ENABLE_AGENT_RELAY:-false}" == "true" ]] && echo " Agent chat relay: http://localhost:${AGENT_RELAY_PORT:-8645}/health"
echo ""
echo " Press Ctrl+C to stop all processes."
echo "══════════════════════════════════════════════════════"
echo ""

wait "${PIDS[@]}"
