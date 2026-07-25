#!/usr/bin/env bash
# =============================================================================
# deploy.sh — non-interactive bare-metal deploy for Hermes Agent runtime.
#
# Assumes passwordless sudo is available for the invoking user (either
# NOPASSWD sudoers rule, or a prior `sudo -v` credential-cache). Preflight
# fails fast with an actionable message if not — no interactive prompts,
# no password transfer through env, no waiting on stdin.
#
# Parallel to hermes_deploy.sh (which uses `sudo` with implicit interactive
# fallback). Ships alongside it, does not replace it. Pick based on runtime:
#   - hermes_deploy.sh: interactive-capable operator, allows sudo prompt
#   - deploy.sh:         non-interactive agent (Hermes runtime, CI, cron)
#
# Usage:
#   bash deploy.sh /path/to/deploy.conf
#
# Full output tee'd to /tmp/deploy-<TAG>-<pid>.log for post-hoc inspection.
# =============================================================================
set -euo pipefail

CONF="${1:-}"
[[ -n "$CONF" && -f "$CONF" ]] || { echo "usage: $0 /path/to/deploy.conf" >&2; exit 2; }
# shellcheck source=/dev/null
. "$CONF"

for v in TAG TARBALL_PATH APP_DIR APP_USER; do
    [[ -n "${!v:-}" ]] || { echo "ERROR: config missing $v" >&2; exit 2; }
done

LOG="/tmp/deploy-$TAG-$$.log"
exec > >(tee -a "$LOG") 2>&1

# ── Phase 0: non-interactive sudo preflight ─────────────────────────────────
# Single actionable exit path. Do NOT retry, do NOT prompt.
if [[ $EUID -ne 0 ]]; then
    if ! sudo -n true 2>/dev/null; then
        cat >&2 <<EOF
ERROR: passwordless sudo not available for user '$USER' on $(hostname).
This script is non-interactive and cannot supply a password.

Fix (one-time, as root on this host):
  echo '$USER ALL=(ALL) NOPASSWD:ALL' | tee /etc/sudoers.d/hermes-deploy
  chmod 440 /etc/sudoers.d/hermes-deploy

Or cache credentials via interactive terminal BEFORE re-invoking:
  sudo -v && bash $0 $CONF

Details:
  hostname:  $(hostname)
  user:      $USER (uid=$EUID)
  tag:       $TAG
  conf:      $CONF
EOF
        exit 1
    fi
    SUDO="sudo -n"
else
    SUDO=""
fi

# Silence apt/dpkg interactive prompts
export DEBIAN_FRONTEND=noninteractive

# ── Phase 1: extract tarball ─────────────────────────────────────────────────
[[ -f "$TARBALL_PATH" ]] || { echo "ERROR: tarball not found at $TARBALL_PATH" >&2; exit 1; }
STAGE="/tmp/deploy-$TAG-$$-stage"
mkdir -p "$STAGE"
tar -xzf "$TARBALL_PATH" -C "$STAGE"
EXTRACTED="$STAGE/antigravity-predictor-bare-metal-$TAG"
[[ -d "$EXTRACTED" ]] || { echo "ERROR: extraction did not produce $EXTRACTED" >&2; exit 1; }

# ── Phase 2: install (delegates to install.sh, which handles its own deps) ──
$SUDO bash "$EXTRACTED/deploy/bare-metal/install.sh" \
    --app-dir "$APP_DIR" --user "$APP_USER"

# ── Phase 3: populate .env from optional config vars ─────────────────────────
ENV_FILE="$APP_DIR/.env"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: install.sh did not produce $ENV_FILE" >&2; exit 1; }
for k in ANTHROPIC_API_KEY AGENT_RELAY_CMD SA_INFERENCE_BACKEND OFFSITE_BACKUP_CMD; do
    val="${!k:-}"
    [[ -z "$val" ]] && continue
    esc=$(printf '%s' "$val" | sed 's/[\/&|]/\\&/g')
    $SUDO sed -i "s|^#*$k=.*|$k=$esc|" "$ENV_FILE"
done
$SUDO chown "$APP_USER:$APP_USER" "$ENV_FILE"
$SUDO chmod 600 "$ENV_FILE"

# ── Phase 4: restart services ────────────────────────────────────────────────
$SUDO systemctl restart predictor agent_relay
if [[ "${SA_INFERENCE_BACKEND:-disabled}" == "enabled" ]]; then
    $SUDO systemctl restart signal_agent || echo "WARN: signal_agent restart failed"
fi

# ── Phase 5: minimal verify (services active + api responds) ─────────────────
$SUDO systemctl is-active predictor   >/dev/null || { echo "FAIL: predictor.service not active"; exit 1; }
$SUDO systemctl is-active agent_relay >/dev/null || { echo "FAIL: agent_relay.service not active"; exit 1; }
curl -sf --max-time 5 http://127.0.0.1:18910/api/status >/dev/null || {
    echo "FAIL: /api/status not responding on 127.0.0.1:18910"; exit 1
}

# ── Phase 6: report ──────────────────────────────────────────────────────────
echo "─────────────────────────────────────────────────────────"
echo "[STATUS] deploy-ok"
echo "[TAG] $TAG"
echo "[HOST] $(hostname)"
echo "[APP_DIR] $APP_DIR"
echo "[APP_USER] $APP_USER"
echo "[LOG] $LOG"
$SUDO grep 'Basic auth enabled' "$LOG" 2>/dev/null | tail -1 || echo "[BASIC_AUTH] not found in log (may be idempotent-skip re-install)"
$SUDO systemctl status predictor --no-pager 2>/dev/null | head -3
echo "─────────────────────────────────────────────────────────"

rm -rf "$STAGE" 2>/dev/null || true
