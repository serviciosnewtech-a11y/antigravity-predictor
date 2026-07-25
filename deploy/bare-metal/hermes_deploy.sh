#!/usr/bin/env bash
# =============================================================================
# hermes_deploy.sh — extract tarball, run install.sh, populate .env.
#
# Simplified from the .25 version. install.sh does its own root check, apt
# install, venv setup, systemd wiring, nginx, ufw — this wrapper only does
# what install.sh doesn't: extract the tarball, invoke install.sh with the
# right flags, then populate .env from the config file. Uses sudo transparently
# when not already root instead of hard-failing on non-root — install.sh will
# tell you if sudo is broken.
#
# Usage:
#   bash hermes_deploy.sh /path/to/deploy.conf
# =============================================================================
set -euo pipefail

CONF="${1:-}"
[[ -n "$CONF" && -f "$CONF" ]] || { echo "usage: $0 /path/to/deploy.conf" >&2; exit 2; }
# shellcheck source=/dev/null
. "$CONF"

# Required config vars
for v in TAG TARBALL_PATH APP_DIR APP_USER; do
    [[ -n "${!v:-}" ]] || { echo "ERROR: config missing $v"; exit 2; }
done

# Sudo prefix only when we aren't already root
SUDO=""
[[ $EUID -eq 0 ]] || SUDO="sudo"

# Extract tarball to a scratch dir
STAGE="/tmp/hermes-deploy-$TAG-$$"
mkdir -p "$STAGE"
tar -xzf "$TARBALL_PATH" -C "$STAGE"
EXTRACTED="$STAGE/antigravity-predictor-bare-metal-$TAG"
[[ -d "$EXTRACTED" ]] || { echo "ERROR: tarball did not extract as $EXTRACTED"; exit 1; }

# Run install.sh — handles its own root check, deps, venv, systemd, nginx, ufw
$SUDO bash "$EXTRACTED/deploy/bare-metal/install.sh" --app-dir "$APP_DIR" --user "$APP_USER"

# Populate .env from any optional config vars that were set
ENV_FILE="$APP_DIR/.env"
for k in ANTHROPIC_API_KEY AGENT_RELAY_CMD SA_INFERENCE_BACKEND OFFSITE_BACKUP_CMD; do
    val="${!k:-}"
    [[ -z "$val" ]] && continue
    # Escape sed replacement metachars
    esc=$(printf '%s' "$val" | sed 's/[\/&|]/\\&/g')
    $SUDO sed -i "s|^#*$k=.*|$k=$esc|" "$ENV_FILE"
done
$SUDO chown "$APP_USER:$APP_USER" "$ENV_FILE"
$SUDO chmod 600 "$ENV_FILE"

# Restart services that read .env
$SUDO systemctl restart predictor agent_relay
[[ "${SA_INFERENCE_BACKEND:-disabled}" == "enabled" ]] && $SUDO systemctl restart signal_agent || true

# Print the basic-auth password (from install.sh's log line) + service status
$SUDO journalctl -u predictor.service --since "5 minutes ago" --no-pager 2>/dev/null | grep 'Basic auth' | tail -1 || \
    grep 'Basic auth' /tmp/*.log 2>/dev/null | tail -1 || true
$SUDO systemctl status predictor --no-pager | head -3

# Cleanup staging
rm -rf "$STAGE" 2>/dev/null || true

echo "deploy complete: $TAG at $APP_DIR"
