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
apt-get install -y -qq python3 python3-pip python3-venv nginx certbot python3-certbot-nginx git apache2-utils

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

# Force server.host to loopback for THIS product specifically. The repo's
# config.json ships "0.0.0.0" because that's correct and necessary for
# Docker (the container binds all interfaces internally; docker-compose.yml
# never publishes predictor's own port to the host at all -- nginx is the
# only container with a published port). Bare-metal has no such network
# isolation: "0.0.0.0" here means predictor_server.py listens on the VPS's
# real public interface directly, on plain HTTP, with zero authentication,
# completely bypassing nginx (and whatever TLS/auth/rate-limiting it's
# configured with) for anyone who reaches the port directly. Every other
# bare-metal file (nginx.conf, signal_agent.service) already assumes
# 127.0.0.1-only reachability -- this makes that assumption actually true.
# Found 2026-07-23 auditing what "safely expose the dashboard" requires.
sed -i 's/"host": *"0\.0\.0\.0"/"host": "127.0.0.1"/' "$APP_DIR/config.json"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod +x "$APP_DIR/retrain_all.sh"

# ── Python venv ───────────────────────────────────────────────────────────────
log "Setting up Python venv…"
python3 -m venv "$APP_DIR/.venv"
# Verify venv actually created before continuing -- addresses the §7.7
# recurring failure mode where a partial/silent venv creation left
# predictor.service crash-looping on 203/EXEC. This makes it fail here,
# with an actionable message, instead of much later at service start
# with a cryptic exit code. Root cause of the underlying failure is
# still unknown -- if this triggers, capture the previous few lines of
# install.sh output to help diagnose (disk full? python3-venv package
# broken? permission on APP_DIR? -- all real hypotheses, none confirmed).
[[ -x "$APP_DIR/.venv/bin/python" ]] || die "venv creation failed: $APP_DIR/.venv/bin/python is missing or not executable. Check disk space, that python3-venv is installed (apt install python3-venv), and that $APP_DIR is writable by root."
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

# predictor_backup — periodic durable backup of signal_history.db (the only
# record of every signal/trade the predictor has ever produced) to a
# directory OUTSIDE $APP_DIR, so it survives a bad reinstall, an accidental
# `rm -rf` of the app dir, or standing up fresh on different hardware with
# no path to bring old data along -- the exact incident that prompted this,
# found live 2026-07-23. See tools/backup_signal_log.py's docstring for the
# full reasoning and BACKUP_DIR/BACKUP_RETENTION_COUNT overrides.
BACKUP_DIR="$(dirname "$APP_DIR")/$(basename "$APP_DIR")-backups"
mkdir -p "$BACKUP_DIR"
chown "$APP_USER:$APP_USER" "$BACKUP_DIR"
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/predictor_backup.service" > /etc/systemd/system/predictor_backup.service
cp "$APP_DIR/deploy/bare-metal/predictor_backup.timer" /etc/systemd/system/predictor_backup.timer

# forge_backup -- periodic durable backup of forge.db (paper-trade log +
# scorecard/evaluation_history). Same target directory as
# predictor_backup ($BACKUP_DIR); filenames self-identify. Added
# beta-1.10.16 after §7.10 made forge.db carry evaluation trajectory
# worth preserving.
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/forge_backup.service" > /etc/systemd/system/forge_backup.service
cp "$APP_DIR/deploy/bare-metal/forge_backup.timer" /etc/systemd/system/forge_backup.timer

# config_backup -- periodic durable backup of the "unprotected" sources
# called out as coverage=none in docs/DATA_INVENTORY.md: .env, htpasswd,
# persona memory, config.json, model_*.txt, model metadata/metrics
# reports. Same $BACKUP_DIR as predictor_backup / forge_backup; bundled as
# one configstate.<stamp>.tar.gz per run (they change together, restoring
# piecewise is more error-prone than as a set). 12h cadence, distinct
# from the SQLite backups' 6h. Added beta-1.10.21.
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/config_backup.service" > /etc/systemd/system/config_backup.service
cp "$APP_DIR/deploy/bare-metal/config_backup.timer" /etc/systemd/system/config_backup.timer

# sync_offsite -- push /opt/predictor-backups to an operator-configured
# off-host destination. The "how" is OFFSITE_BACKUP_CMD (see .env.example
# for rclone / rsync / aws / azcopy examples). Timer is enabled by
# default; when OFFSITE_BACKUP_CMD is unset the service exits 0 with a
# "not configured, skipping" log line so this never fail-loops on a fresh
# install. See tools/sync_backups_offsite.py + HANDOFF §7.17. Added
# beta-1.10.22.
sed "s|/opt/predictor|$APP_DIR|g; s|User=predictor|User=$APP_USER|g; s|Group=predictor|Group=$APP_USER|g" \
    "$APP_DIR/deploy/bare-metal/sync_offsite.service" > /etc/systemd/system/sync_offsite.service
cp "$APP_DIR/deploy/bare-metal/sync_offsite.timer" /etc/systemd/system/sync_offsite.timer

# forge_scorecard -- periodic evaluation pass over Forge trade history.
# Reads forge_data/forge.db, writes strategy_scorecard + evaluation_history,
# dumps a plain-language text summary to a directory OUTSIDE $APP_DIR (same
# principle as $BACKUP_DIR above: the operator-facing view should survive a
# wipe of the app dir). See forge/scoring.py + tools/forge_scorecard.py for
# the metric set, verdict thresholds, and env-var overrides.
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
# Every route in predictor_server.py is unauthenticated by design (it's a
# single-operator advisory tool, not a multi-tenant SaaS) -- fine as long as
# nothing outside 127.0.0.1 can reach it, but the moment this goes on the
# public internet (see the firewall section below and nginx.conf), anyone
# who finds the IP/domain gets full access to the dashboard, API, and chat
# with zero login wall. This is a stopgap, not a real access-control system:
# one shared username/password for everyone who's given it, enforced by
# nginx before a request ever reaches predictor_server.py. Fine for "let a
# few people poke around and find bugs"; NOT sufficient for real per-user
# accounts/audit trails -- that needs actual app-level auth, a bigger,
# separate piece of work. ENABLE_BASIC_AUTH=false skips this entirely (e.g.
# for a purely local/VPN-only install that doesn't want a password wall).
# Idempotent: leaves an existing /etc/nginx/.htpasswd alone on a rerun
# rather than silently rotating credentials shared testers already have.
ENABLE_BASIC_AUTH="${ENABLE_BASIC_AUTH:-true}"
HTPASSWD_FILE=/etc/nginx/.htpasswd
if [[ "$ENABLE_BASIC_AUTH" == "true" ]]; then
    if [[ ! -f "$HTPASSWD_FILE" ]]; then
        BASIC_AUTH_USER="${BASIC_AUTH_USER:-predictor}"
        BASIC_AUTH_PASS="${BASIC_AUTH_PASS:-$(openssl rand -base64 18 | tr -d '=+/' | head -c 20)}"
        htpasswd -cb "$HTPASSWD_FILE" "$BASIC_AUTH_USER" "$BASIC_AUTH_PASS" >/dev/null
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
# Defense in depth on top of the config.json loopback-bind fix above: even
# with predictor_server.py correctly bound to 127.0.0.1, nothing was ever
# actually blocking direct external access to it (or to agent_relay's 8645,
# or any other port) at the host level. ufw here is deliberately minimal —
# SSH plus whatever nginx needs — everything else stays denied by default.
# Skips cleanly if ufw isn't installed rather than failing the whole install.
if command -v ufw &>/dev/null; then
    log "Configuring firewall (ufw): allowing SSH, HTTP, HTTPS only…"
    ufw allow OpenSSH    >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1
    ufw allow 80/tcp     >/dev/null 2>&1
    ufw allow 443/tcp    >/dev/null 2>&1
    ufw --force enable   >/dev/null 2>&1
    log "Firewall enabled — only 22/80/443 reachable from outside this host."
else
    log "WARN: ufw not found — skipping firewall setup. Ports 18910/8645 are" \
        "only bound to 127.0.0.1 (see config.json fix above), but with no" \
        "host firewall at all, confirm your cloud provider's own security" \
        "group/network ACL restricts inbound traffic before exposing this" \
        "host publicly."
fi

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
