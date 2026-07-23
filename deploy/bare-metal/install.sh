#!/usr/bin/env bash
# =============================================================================
# install.sh — Deploy Antigravity Predictor on a fresh Ubuntu 22.04 VPS
#
# Run as root:
#   bash install.sh [--app-dir /opt/predictor] [--user predictor]
# =============================================================================

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/predictor}"
APP_USER="${APP_USER:-predictor}"
REPO_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { echo "[INSTALL] $*"; }
die() { echo "[ERROR] $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root."

# ── System deps ───────────────────────────────────────────────────────────────
log "Installing system packages…"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx certbot python3-certbot-nginx git

# ── App user ──────────────────────────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    log "Creating user $APP_USER…"
    useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" --create-home "$APP_USER"
fi

# ── Copy app files ────────────────────────────────────────────────────────────
log "Copying app to $APP_DIR…"
mkdir -p "$APP_DIR"/{src,models,dashboard,tools,agent_state,data/{raw,macro,datasets},logs,deploy/bare-metal}

rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
    "$REPO_SRC/src/"     "$APP_DIR/src/"
rsync -a "$REPO_SRC/models/"  "$APP_DIR/models/" 2>/dev/null || true
rsync -a "$REPO_SRC/deploy/bare-metal/"  "$APP_DIR/deploy/bare-metal/"
# tools/ — needed for agent_chat_relay.py (the local, no-API-key CLI-agent
# chat backend; see agent_relay.service below). Previously not copied at
# all since nothing in the systemd product used anything from tools/ yet.
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$REPO_SRC/tools/"  "$APP_DIR/tools/"
# dashboard/ (index.html/app.js/style.css) — predictor_server.py mounts this
# via FastAPI StaticFiles at "/". Without this copy the mount silently no-ops
# (guarded by an os.path.exists check) and nginx's "location /" proxy gets a
# 404 from FastAPI for every request — the dashboard would never load on a
# fresh install. Found during pre-redeploy verification, not a live incident.
rsync -a "$REPO_SRC/dashboard/"  "$APP_DIR/dashboard/"
cp    "$REPO_SRC/retrain_all.sh" "$APP_DIR/"
cp    "$REPO_SRC/requirements.txt" "$APP_DIR/" 2>/dev/null || true
# config.json — predictor_server.py hard-requires this (raises and refuses to
# start if missing from both src/config.json and $APP_DIR/config.json). This
# copy step was missing entirely, which would have crash-looped predictor.service
# on every fresh install. Found during pre-redeploy verification.
cp    "$REPO_SRC/config.json" "$APP_DIR/config.json"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod +x "$APP_DIR/retrain_all.sh"

# ── Python venv ───────────────────────────────────────────────────────────────
log "Setting up Python venv…"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
if [[ -f "$APP_DIR/requirements.txt" ]]; then
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
else
    "$APP_DIR/.venv/bin/pip" install -q \
        lightgbm pandas numpy scikit-learn pyarrow fastapi uvicorn \
        websockets loguru requests ccxt yfinance \
        anthropic duckduckgo-search
fi
log "Python deps installed."

# ── .env file (API keys) ──────────────────────────────────────────────────────
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    log "Creating .env template at $ENV_FILE — fill in ANTHROPIC_API_KEY before starting signal_agent."
    cat > "$ENV_FILE" <<'EOF'
# Antigravity Predictor — environment variables
# Fill in before starting services. Keep this file chmod 600.
# predictor.service, signal_agent.service, and agent_relay.service all load
# this file (EnvironmentFile=-/opt/predictor/.env) — one file, all three
# processes, no separate config to keep in sync between them.

# ── Hermes brain backend (shared by predictor.service's /api/chat AND ───────
# ── signal_agent's automated signal-triggered enrichment) ───────────────────
# One backend config answers both: the dashboard's interactive chat and the
# automated note generated when a signal crosses threshold (SA_INFERENCE_
# BACKEND below must also be "enabled" for the second one to actually run).
#
# Ships pointed at the local agent relay by default (Pattern B below) — the
# real local Hermes agent answers using its own session/context, not a
# stateless hosted API call. Point HERMES_PROXY_URL at a remote OpenAI-
# compatible API instead, or set CHAT_BACKEND=anthropic + ANTHROPIC_API_KEY,
# if you'd rather use a hosted provider — no code changes needed either way.
#
# CHAT_BACKEND=            "hermes_proxy" | "anthropic" | "ollama" | blank
# Left blank (default), auto-detects: HERMES_PROXY_URL -> ANTHROPIC_API_KEY
# (native Anthropic Messages API) -> OLLAMA_URL. Set explicitly to force
# exactly one and skip the others.
CHAT_BACKEND=
ANTHROPIC_CHAT_MODEL=

# Pattern B (default) — local CLI-agent relay, no API key needed.
# agent_relay.service runs tools/agent_chat_relay.py, which shells out to
# whatever AGENT_RELAY_CMD names (any CLI-based agent, not just Hermes) and
# relays predictor's HERMES_PROXY_URL calls to it. HERMES_PROXY_URL below
# points at that relay by default — change AGENT_RELAY_CMD to point at
# whatever agent binary is actually installed on this host; verify with
# `sudo -u predictor /opt/predictor/.venv/bin/python
# /opt/predictor/tools/agent_chat_relay.py` and curl its /health before
# trusting agent_relay.service to be doing anything useful.
HERMES_PROXY_URL=http://127.0.0.1:8645
HERMES_INFERENCE_MODEL=
AGENT_RELAY_CMD=
AGENT_RELAY_PORT=8645

# Pattern A — remote OpenAI-compatible API instead of the local relay:
# leave HERMES_PROXY_URL pointed at the remote API's URL and set
# HERMES_PROXY_API_KEY.
HERMES_PROXY_API_KEY=

# Optional fallbacks if HERMES_PROXY_URL is unreachable:
OLLAMA_URL=
OLLAMA_MODEL=llama3.1
ANTHROPIC_API_KEY=

# ── Automated signal-triggered enrichment ────────────────────────────────────
# On/off switch only — which backend answers is CHAT_BACKEND/HERMES_PROXY_URL/
# ANTHROPIC_API_KEY/OLLAMA_URL above, the SAME config the chat uses (one
# shared Hermes brain, not two independent configs to keep in sync). Default
# is disabled. Set to "enabled" to turn it on.
SA_INFERENCE_BACKEND=disabled

# Confidence threshold calibrated to actual LightGBM output range (0.18-0.28).
# Do NOT restore to 0.65 - it will never fire.
SA_CONFIDENCE_THRESHOLD=0.22
SA_COOLDOWN_SECONDS=900
SA_POLL_INTERVAL=30

# ── Optional predictor URL override ───────────────────────────────────────────
# PREDICTOR_URL=http://127.0.0.1:18910
EOF
    chmod 600 "$ENV_FILE"
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
else
    log ".env already exists — skipping template creation."
fi

# ── Systemd services ──────────────────────────────────────────────────────────
log "Installing systemd units…"

# Fix ExecStart path in service files to match APP_DIR
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/predictor.service" > /etc/systemd/system/predictor.service

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/macro_refresh.service" > /etc/systemd/system/macro_refresh.service

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/signal_agent.service" > /etc/systemd/system/signal_agent.service

# agent_relay.service — local, no-API-key CLI-agent chat backend
# (tools/agent_chat_relay.py). Always installed/enabled: it degrades
# gracefully (health-check reports agent_binary_exists=false, /api/chat
# calls return a real 502 instead of a crash) if AGENT_RELAY_CMD's binary
# isn't actually on this host, so there's no harm in it always running —
# but it does mean it's only USEFUL once .env's AGENT_RELAY_CMD points at
# a real installed agent. See the .env template below.
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/agent_relay.service" > /etc/systemd/system/agent_relay.service

cp "$APP_DIR/deploy/bare-metal/macro_refresh.timer" /etc/systemd/system/macro_refresh.timer

systemctl daemon-reload
systemctl enable predictor macro_refresh.timer signal_agent agent_relay
log "Services enabled."

# ── Nginx ─────────────────────────────────────────────────────────────────────
log "Configuring nginx…"
cp "$APP_DIR/deploy/bare-metal/nginx.conf" /etc/nginx/sites-available/predictor
ln -sf /etc/nginx/sites-available/predictor /etc/nginx/sites-enabled/predictor
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── Initial macro fetch ───────────────────────────────────────────────────────
log "Running initial macro data fetch…"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/src/fetch_macro.py" \
    --data-dir "$APP_DIR/data/macro" --days 730 || \
    log "WARN: initial macro fetch failed — run manually before retraining."

# ── Start services ────────────────────────────────────────────────────────────
log "Starting predictor, macro timer, and agent relay…"
systemctl start predictor
systemctl start macro_refresh.timer
# Always started — degrades gracefully (see agent_relay.service comment
# above) rather than crashing if AGENT_RELAY_CMD's binary isn't installed.
systemctl start agent_relay

# signal_agent's on/off switch is SA_INFERENCE_BACKEND, not a specific key —
# it shares whatever backend the chat is configured with (CHAT_BACKEND/
# HERMES_PROXY_URL/ANTHROPIC_API_KEY/OLLAMA_URL above), so presence of one
# specific var (the old check) is the wrong signal now. Start it now — it
# will log an error and stay idle if the configured backend isn't actually
# reachable, but it won't crash.
SA_BACKEND_VALUE="$(grep -E '^SA_INFERENCE_BACKEND=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
if [[ -n "$SA_BACKEND_VALUE" && "$SA_BACKEND_VALUE" != "disabled" ]]; then
    systemctl start signal_agent
    log "signal_agent.service started."
else
    log "WARN: SA_INFERENCE_BACKEND is disabled (or unset) in $ENV_FILE — signal_agent NOT started."
    log "      Edit $ENV_FILE (SA_INFERENCE_BACKEND=enabled), then: systemctl start signal_agent"
fi

log ""
log "======================================================"
log " Antigravity Predictor installed successfully."
log ""
log " API:       http://<vps-ip>/api/status"
log " Dashboard: http://<vps-ip>/"
log " WebSocket: ws://<vps-ip>/ws"
log ""
log " Logs:      journalctl -u predictor -f"
log "            tail -f $APP_DIR/logs/predictor.log"
log ""
log " Signal agent: systemctl status signal_agent"
log "              journalctl -u signal-agent -f"
log ""
log " Agent relay (chat backend): systemctl status agent_relay"
log "                             curl http://127.0.0.1:8645/health"
log "                             journalctl -u agent-relay -f"
log " Chat status: curl http://127.0.0.1:18910/api/chat/status"
log ""
log " Backend config: edit $APP_DIR/.env then restart predictor/signal_agent/agent_relay"
log " Retrain:  cd $APP_DIR && bash retrain_all.sh"
log " Status:   systemctl status predictor"
log "======================================================"
