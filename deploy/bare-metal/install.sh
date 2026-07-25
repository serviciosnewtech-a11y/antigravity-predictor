#!/usr/bin/env bash
# =============================================================================
# install.sh — Deploy Antigravity Predictor on a fresh Ubuntu 22.04 host
#
# Two equivalent invocations (either flags or env vars, matching values):
#
#   sudo bash install.sh [--app-dir /opt/predictor] [--user predictor]
#
#   APP_DIR=/opt/predictor APP_USER=predictor sudo -E bash install.sh
#
# Flags OVERRIDE env vars if both are set (CLI-wins). Defaults: /opt/predictor
# and 'predictor'. If APP_USER doesn't exist, it's created as a system user;
# if it already exists (e.g. a custom install into a real user's home dir),
# it's used as-is with no re-creation. --help prints usage and exits.
# =============================================================================

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/predictor}"
APP_USER="${APP_USER:-predictor}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app-dir)  APP_DIR="$2";  shift 2 ;;
        --user)     APP_USER="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's|^# \?||'
            exit 0 ;;
        *)  echo "[ERROR] Unknown argument: $1" >&2
            echo "        Run '$0 --help' for usage." >&2
            exit 2 ;;
    esac
done

REPO_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { echo "[INSTALL] $*"; }
die() { echo "[ERROR] $*"; exit 1; }

# ── System deps ───────────────────────────────────────────────────────────────
log "Installing system packages…"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx certbot python3-certbot-nginx git apache2-utils acl

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
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$REPO_SRC/tools/"  "$APP_DIR/tools/"
rsync -a "$REPO_SRC/dashboard/"  "$APP_DIR/dashboard/"
cp    "$REPO_SRC/retrain_all.sh" "$APP_DIR/"
cp    "$REPO_SRC/requirements.txt" "$APP_DIR/" 2>/dev/null || true
cp    "$REPO_SRC/config.json" "$APP_DIR/config.json"

sed -i 's/"host": *"0\.0\.0\.0"/"host": "127.0.0.1"/' "$APP_DIR/config.json"

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
#
# Three real gotchas found during live deploys, see
# deploy/bare-metal/LIVE_DEPLOY_NOTES.md for the full story:
#  1. Use an ABSOLUTE PATH to the agent binary, not a bare command name --
#     systemd services don't inherit an interactive shell's PATH, so
#     `command -v <binary>` works fine by hand but resolves to nothing here.
#  2. Do NOT wrap {prompt} in your own quotes (e.g. '/bin/echo "reply:
#     {prompt}"' is WRONG) -- the relay's own substitution already quotes
#     it safely; nesting breaks the moment a real prompt contains an
#     embedded quote character, which it will (health-check probes are
#     quote-free and pass fine, hiding this until a real chat request).
#     Template shape:  AGENT_RELAY_CMD=/path/to/binary --some-flag {prompt}
#  3. "--profile <name>" only works if that Hermes CLI profile already
#     exists on THIS host -- don't copy the "metis" example verbatim and
#     assume it works. Confirmed broken on a live host 2026-07-23 (see
#     LIVE_DEPLOY_NOTES.md #7): always run the exact AGENT_RELAY_CMD by hand
#     with a real prompt first, then set it here once it's proven to work.
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

# ── Off-site backup sync (sync_offsite.timer) ────────────────────────────────
# The "how to push" seam for tools/sync_backups_offsite.py. Unset by
# default -- the timer stays on but the service exits 0 with a clear
# "not configured, skipping" log line so it doesn't fail-loop. When
# ready, set this to whatever pushes /opt/predictor-backups to your
# off-host destination of choice. Examples (uncomment ONE, adjust the
# destination):
#
# OFFSITE_BACKUP_CMD='rclone copy /opt/predictor-backups mydrive:predictor-backups'
# OFFSITE_BACKUP_CMD='rsync -av --delete /opt/predictor-backups/ user@offsite:predictor-backups/'
# OFFSITE_BACKUP_CMD='aws s3 sync /opt/predictor-backups s3://my-bucket/predictor-backups'
# OFFSITE_BACKUP_CMD='azcopy sync /opt/predictor-backups https://my.blob.core.windows.net/predictor-backups'
#
# Argv-parsed (shlex.split), not shell-executed -- no metacharacter
# injection surface. See tools/sync_backups_offsite.py for the full
# design.
OFFSITE_BACKUP_CMD=
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

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/agent_relay.service" > /etc/systemd/system/agent_relay.service

cp "$APP_DIR/deploy/bare-metal/macro_refresh.timer" /etc/systemd/system/macro_refresh.timer

BACKUP_DIR="$(dirname "$APP_DIR")/$(basename "$APP_DIR")-backups"
mkdir -p "$BACKUP_DIR"
chown "$APP_USER:$APP_USER" "$BACKUP_DIR"
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/predictor_backup.service" > /etc/systemd/system/predictor_backup.service
cp "$APP_DIR/deploy/bare-metal/predictor_backup.timer" /etc/systemd/system/predictor_backup.timer

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/forge_backup.service" > /etc/systemd/system/forge_backup.service
cp "$APP_DIR/deploy/bare-metal/forge_backup.timer" /etc/systemd/system/forge_backup.timer

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/config_backup.service" > /etc/systemd/system/config_backup.service
cp "$APP_DIR/deploy/bare-metal/config_backup.timer" /etc/systemd/system/config_backup.timer

sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/sync_offsite.service" > /etc/systemd/system/sync_offsite.service
cp "$APP_DIR/deploy/bare-metal/sync_offsite.timer" /etc/systemd/system/sync_offsite.timer

FORGE_SCORECARD_DIR="$(dirname "$APP_DIR")/$(basename "$APP_DIR")-forge-scorecard"
mkdir -p "$FORGE_SCORECARD_DIR"
chown "$APP_USER:$APP_USER" "$FORGE_SCORECARD_DIR"
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/forge_scorecard.service" > /etc/systemd/system/forge_scorecard.service
cp "$APP_DIR/deploy/bare-metal/forge_scorecard.timer" /etc/systemd/system/forge_scorecard.timer

systemctl daemon-reload
systemctl enable predictor macro_refresh.timer signal_agent agent_relay predictor_backup.timer forge_backup.timer config_backup.timer sync_offsite.timer forge_scorecard.timer
log "Services enabled."

# ── Basic auth ────────────────────────────────────────────────────────────────
ENABLE_BASIC_AUTH="${ENABLE_BASIC_AUTH:-true}"
HTPASSWD_FILE=/etc/nginx/.htpasswd
if [[ "$ENABLE_BASIC_AUTH" == "true" ]]; then
    if [[ ! -f "$HTPASSWD_FILE" ]]; then
        BASIC_AUTH_USER="${BASIC_AUTH_USER:-predictor}"
        BASIC_AUTH_PASS="${BASIC_AUTH_PASS:-$(openssl rand -base64 18 | tr -d '=+/' | head -c 20)}"
        htpasswd -cb "$HTPASSWD_FILE" "$BASIC_AUTH_USER" "$BASIC_AUTH_PASS" >/dev/null
        # Parseable form for automated deploy scripts (deploy.sh greps for this exact line).
        # Also print the human-friendly form for interactive operators.
        echo "[BASIC_AUTH] user=$BASIC_AUTH_USER password=$BASIC_AUTH_PASS"
        log "Basic auth enabled — user: $BASIC_AUTH_USER  password: $BASIC_AUTH_PASS"
        log "  (save this now — it is only printed once; rotate later with: htpasswd $HTPASSWD_FILE $BASIC_AUTH_USER)"
    else
        log "Basic auth: $HTPASSWD_FILE already exists — leaving existing credentials in place."
    fi
else
    log "Basic auth disabled (ENABLE_BASIC_AUTH=false) — dashboard will have no login wall once exposed."
fi

# ── Nginx ─────────────────────────────────────────────────────────────────────
log "Configuring nginx…"
cp "$APP_DIR/deploy/bare-metal/nginx.conf" /etc/nginx/sites-available/predictor
if [[ "$ENABLE_BASIC_AUTH" != "true" ]]; then
    # Strip the auth_basic lines nginx.conf ships with by default.
    sed -i '/auth_basic/d' /etc/nginx/sites-available/predictor
fi
ln -sf /etc/nginx/sites-available/predictor /etc/nginx/sites-enabled/predictor
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── Firewall ──────────────────────────────────────────────────────────────────
log "Configuring firewall (ufw): allowing SSH, HTTP, HTTPS only…"
ufw allow OpenSSH    >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1
ufw allow 80/tcp     >/dev/null 2>&1
ufw allow 443/tcp    >/dev/null 2>&1
ufw --force enable   >/dev/null 2>&1
log "Firewall enabled — only 22/80/443 reachable from outside this host."

# ── Initial macro fetch ───────────────────────────────────────────────────────
log "Running initial macro data fetch…"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/src/fetch_macro.py" \
    --data-dir "$APP_DIR/data/macro" --days 730 || \
    log "WARN: initial macro fetch failed — run manually before retraining."

# ── Start services ────────────────────────────────────────────────────────────
log "Starting predictor, macro timer, backup timers, forge scorecard timer, and agent relay…"
systemctl start predictor
systemctl start macro_refresh.timer
systemctl start predictor_backup.timer
systemctl start forge_backup.timer
systemctl start config_backup.timer
systemctl start sync_offsite.timer
systemctl start forge_scorecard.timer
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
# ── Inspector-user ACL grant ─────────────────────────────────────────────────
# 0700 on $APP_DIR (predictor's private home dir) blocks anyone but predictor
# and root from reading the app state, which means every diagnostic run by
# another account (operator, Hermes agent, monitoring tooling) needs a sudo
# wrapper. Grant read+traverse ACL to one specific "inspector" account so it
# can `cat`/`ls`/`sqlite3`/`tail` without sudo. Mutating operations still
# need sudo — this is read-only. Alternative to loosening the private
# 0700 (which would leak secrets in .env). See HANDOFF §7.7 friction notes.
#
# Auto-detects INSPECTOR_USER from SUDO_USER (the user who invoked sudo).
# Override with e.g. INSPECTOR_USER=hermes if Hermes runs as a different
# account than the operator who launched install.sh.
INSPECTOR_USER="${INSPECTOR_USER:-${SUDO_USER:-}}"
if [[ -n "$INSPECTOR_USER" && "$INSPECTOR_USER" != "$APP_USER" && "$INSPECTOR_USER" != "root" ]]; then
    if id "$INSPECTOR_USER" &>/dev/null; then
        log "Granting read+traverse ACL to inspector user: $INSPECTOR_USER"
        setfacl -Rm  "u:$INSPECTOR_USER:rX" "$APP_DIR"
        setfacl -dRm "u:$INSPECTOR_USER:rX" "$APP_DIR"
        for extra in "$BACKUP_DIR" "$FORGE_SCORECARD_DIR"; do
            [[ -d "$extra" ]] || continue
            setfacl -Rm  "u:$INSPECTOR_USER:rX" "$extra"
            setfacl -dRm "u:$INSPECTOR_USER:rX" "$extra"
        done
        log "  (mutation still requires sudo — this grant is read-only)"
    else
        log "WARN: INSPECTOR_USER='$INSPECTOR_USER' does not exist; skipping ACL grant"
    fi
else
    log "No INSPECTOR_USER to grant read ACL (SUDO_USER=${SUDO_USER:-<unset>}, APP_USER=$APP_USER)"
    log "  To add later:  sudo setfacl -Rm u:<user>:rX $APP_DIR"
fi

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
log ""
if [[ "$ENABLE_BASIC_AUTH" == "true" ]]; then
    log " Dashboard access: username/password required (see 'Basic auth enabled' line"
    log "                   above for credentials — only printed once, on first install)."
    log "                   Rotate:  htpasswd $HTPASSWD_FILE <user>"
    log "                   Disable: remove 'auth_basic*' lines from"
    log "                            /etc/nginx/sites-available/predictor, then: nginx -t && systemctl reload nginx"
else
    log " Dashboard access: NO login wall (ENABLE_BASIC_AUTH=false) — anyone who reaches"
    log "                   this host on 80/443 has full access."
fi
log ""
log " Before exposing this host publicly: point a domain at it and run"
log "   certbot --nginx -d your.domain.com"
log " to enable HTTPS — nginx.conf ships with plain HTTP only until then."
log ""
log " Data backup: signal_history.db backed up every 6h to $BACKUP_DIR"
log "              (deliberately outside $APP_DIR — survives a bad reinstall"
log "              or a wipe of the app directory). Run once now:"
log "                sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/tools/backup_signal_log.py"
log ""
log " Config/secrets backup: .env, htpasswd, config.json, models, persona"
log "              memory bundled every 12h into $BACKUP_DIR as"
log "              configstate.*.tar.gz. Run once now:"
log "                sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/tools/backup_config_and_secrets.py"
log ""
# Off-site sync: report configuration status in the install banner so
# a fresh install makes it obvious this seam exists (and that it's a
# no-op until wired up). The timer starts either way; the service is a
# graceful no-op when OFFSITE_BACKUP_CMD isn't set (see
# tools/sync_backups_offsite.py).
OFFSITE_STATUS_LINE="Off-site sync: unconfigured — set OFFSITE_BACKUP_CMD in $ENV_FILE when ready (see .env template for rclone/rsync/aws/azcopy examples)."
if grep -qE '^OFFSITE_BACKUP_CMD=..*$' "$ENV_FILE" 2>/dev/null; then
    OFFSITE_STATUS_LINE="Off-site sync: OFFSITE_BACKUP_CMD is set — sync_offsite.timer will push $BACKUP_DIR every 6h."
fi
log " $OFFSITE_STATUS_LINE"
log ""
log " Forge scorecard: strategy verdicts computed hourly, dumped to"
log "                  $FORGE_SCORECARD_DIR/scorecard.txt"
log "                  (also outside $APP_DIR). Run once now:"
log "                    sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/tools/forge_scorecard.py"
log "                  Then: cat $FORGE_SCORECARD_DIR/scorecard.txt"
log "                  API:  curl http://127.0.0.1:18912/recommendations"
log "======================================================"
