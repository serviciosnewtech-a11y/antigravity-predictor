#!/usr/bin/env bash
# =============================================================================
# rollback.sh — undo a bare-metal install.
#
# Stops+disables all predictor services and timers, removes systemd unit
# files, removes $APP_DIR. By default PRESERVES:
#   - <APP_DIR>-backups           (durable snapshots)
#   - <APP_DIR>-forge-scorecard   (evaluation history dump)
#   - /etc/nginx/.htpasswd        (basic auth password, printed once)
#   - nginx site config           (operator may host other sites)
#
# Optional flags (can combine):
#   --wipe-backups      also remove <APP_DIR>-backups + <APP_DIR>-forge-scorecard
#   --wipe-htpasswd     also remove /etc/nginx/.htpasswd
#   --wipe-ufw          reset ufw allow list added by install.sh (SSH kept)
#
# Usage:
#   bash rollback.sh /path/to/deploy.conf [--wipe-backups] [--wipe-htpasswd] [--wipe-ufw]
# =============================================================================
set -uo pipefail

CONF="${1:-}"
shift || true
WIPE_BACKUPS=""
WIPE_HTPASSWD=""
WIPE_UFW=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wipe-backups)  WIPE_BACKUPS=1 ;;
        --wipe-htpasswd) WIPE_HTPASSWD=1 ;;
        --wipe-ufw)      WIPE_UFW=1 ;;
        *) echo "ERROR: unknown flag: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ -n "$CONF" && -f "$CONF" ]] || { echo "usage: $0 /path/to/deploy.conf [--wipe-backups] [--wipe-htpasswd] [--wipe-ufw]" >&2; exit 2; }
# shellcheck source=/dev/null
. "$CONF"

for v in APP_DIR APP_USER; do
    [[ -n "${!v:-}" ]] || { echo "ERROR: config missing $v" >&2; exit 2; }
done

SUDO=""
if [[ $EUID -ne 0 ]]; then
    if ! sudo -n true 2>/dev/null; then
        echo "ERROR: passwordless sudo required for rollback." >&2
        echo "  See docs/DEPLOY_NONINTERACTIVE.md for NOPASSWD setup." >&2
        exit 1
    fi
    SUDO="sudo -n"
fi

echo "[rollback] tag context: TAG=${TAG:-<unset>}  APP_DIR=$APP_DIR"

echo "[rollback] stopping + disabling services and timers..."
for u in \
    predictor.service agent_relay.service signal_agent.service \
    predictor_backup.timer forge_backup.timer forge_scorecard.timer \
    config_backup.timer sync_offsite.timer macro_refresh.timer; do
    $SUDO systemctl stop    "$u" 2>/dev/null || true
    $SUDO systemctl disable "$u" 2>/dev/null || true
done

echo "[rollback] removing systemd unit files..."
for u in \
    predictor.service agent_relay.service signal_agent.service \
    predictor_backup.service predictor_backup.timer \
    forge_backup.service forge_backup.timer \
    forge_scorecard.service forge_scorecard.timer \
    config_backup.service config_backup.timer \
    sync_offsite.service sync_offsite.timer \
    macro_refresh.service macro_refresh.timer; do
    $SUDO rm -f "/etc/systemd/system/$u"
done
$SUDO systemctl daemon-reload

echo "[rollback] removing app dir: $APP_DIR"
$SUDO rm -rf "$APP_DIR"

BACKUP_DIR="$(dirname "$APP_DIR")/$(basename "$APP_DIR")-backups"
SCORECARD_DIR="$(dirname "$APP_DIR")/$(basename "$APP_DIR")-forge-scorecard"

if [[ -n "$WIPE_BACKUPS" ]]; then
    echo "[rollback] --wipe-backups: removing $BACKUP_DIR + $SCORECARD_DIR"
    $SUDO rm -rf "$BACKUP_DIR" "$SCORECARD_DIR"
else
    [[ -e "$BACKUP_DIR"    ]] && echo "[rollback] preserved: $BACKUP_DIR (pass --wipe-backups to remove)"
    [[ -e "$SCORECARD_DIR" ]] && echo "[rollback] preserved: $SCORECARD_DIR (pass --wipe-backups to remove)"
fi

if [[ -n "$WIPE_HTPASSWD" ]]; then
    echo "[rollback] --wipe-htpasswd: removing /etc/nginx/.htpasswd"
    $SUDO rm -f /etc/nginx/.htpasswd
elif [[ -e /etc/nginx/.htpasswd ]]; then
    echo "[rollback] preserved: /etc/nginx/.htpasswd (pass --wipe-htpasswd to remove)"
fi

if [[ -n "$WIPE_UFW" ]]; then
    echo "[rollback] --wipe-ufw: removing predictor-related ufw allow rules"
    $SUDO ufw --force delete allow 80/tcp   2>/dev/null || true
    $SUDO ufw --force delete allow 443/tcp  2>/dev/null || true
    # SSH (22 / OpenSSH) intentionally NOT removed — losing it locks out the host
    echo "[rollback] preserved: SSH allow rule (never automatically removed)"
fi

if [[ -e /etc/nginx/sites-available/predictor ]]; then
    echo "[rollback] preserved: /etc/nginx/sites-available/predictor (remove manually if wanted)"
fi
if [[ -L /etc/nginx/sites-enabled/predictor ]]; then
    echo "[rollback] preserved: /etc/nginx/sites-enabled/predictor symlink (remove manually if wanted)"
fi

echo "[rollback] complete."
