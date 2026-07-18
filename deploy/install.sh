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
REPO_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
mkdir -p "$APP_DIR"/{src,models,data/{raw,macro,datasets},logs,deploy}

rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
    "$REPO_SRC/src/"     "$APP_DIR/src/"
rsync -a "$REPO_SRC/models/"  "$APP_DIR/models/" 2>/dev/null || true
rsync -a "$REPO_SRC/deploy/"  "$APP_DIR/deploy/"
cp    "$REPO_SRC/retrain_all.sh" "$APP_DIR/"
cp    "$REPO_SRC/requirements.txt" "$APP_DIR/" 2>/dev/null || true

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

# ── Signal Agent inference backend ────────────────────────────────────────────
# "ollama"  → local Ollama on this host (no API key needed)
# "claude"  → Anthropic API (set ANTHROPIC_API_KEY below)
SA_INFERENCE_BACKEND=ollama
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.1

# Confidence threshold calibrated to actual LightGBM output range (0.18-0.28).
# Do NOT restore to 0.65 - it will never fire.
SA_CONFIDENCE_THRESHOLD=0.22
SA_COOLDOWN_SECONDS=900
SA_POLL_INTERVAL=30

# Required only when SA_INFERENCE_BACKEND=claude:
ANTHROPIC_API_KEY=

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
    "$APP_DIR/deploy/predictor.service" > /etc/systemd/system/predictor.service

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g" \
    "$APP_DIR/deploy/macro_refresh.service" > /etc/systemd/system/macro_refresh.service

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/signal_agent.service" > /etc/systemd/system/signal_agent.service

cp "$APP_DIR/deploy/macro_refresh.timer" /etc/systemd/system/macro_refresh.timer

systemctl daemon-reload
systemctl enable predictor macro_refresh.timer signal_agent
log "Services enabled."

# ── Nginx ─────────────────────────────────────────────────────────────────────
log "Configuring nginx…"
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/predictor
ln -sf /etc/nginx/sites-available/predictor /etc/nginx/sites-enabled/predictor
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── Initial macro fetch ───────────────────────────────────────────────────────
log "Running initial macro data fetch…"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/src/fetch_macro.py" \
    --data-dir "$APP_DIR/data/macro" --days 730 || \
    log "WARN: initial macro fetch failed — run manually before retraining."

# ── Start services ────────────────────────────────────────────────────────────
log "Starting predictor and macro timer…"
systemctl start predictor
systemctl start macro_refresh.timer

# signal_agent needs ANTHROPIC_API_KEY in .env before it's useful.
# Start it now — it will log an error if the key is missing, but won't crash.
if grep -q "ANTHROPIC_API_KEY=." "$ENV_FILE" 2>/dev/null; then
    systemctl start signal_agent
    log "signal_agent.service started."
else
    log "WARN: ANTHROPIC_API_KEY not set in $ENV_FILE — signal_agent NOT started."
    log "      Edit $ENV_FILE, then: systemctl start signal_agent"
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
log " API keys: edit $APP_DIR/.env then restart signal_agent"
log " Retrain:  cd $APP_DIR && bash retrain_all.sh"
log " Status:   systemctl status predictor"
log "======================================================"
