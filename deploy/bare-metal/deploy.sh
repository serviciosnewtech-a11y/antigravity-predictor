#!/usr/bin/env bash
# =============================================================================
# deploy.sh — non-interactive bare-metal deploy for Hermes Agent runtime.
#
# Assumes passwordless sudo is available for the invoking user (either
# NOPASSWD sudoers rule, or a prior `sudo -v` credential-cache). Preflight
# fails fast with an actionable message if not — no interactive prompts,
# no password transfer through env, no waiting on stdin.
#
# Writes ALL output to /tmp/deploy-<TAG>-<pid>.log AND a structured summary
# to /tmp/deploy-<TAG>-report.txt so Hermes can parse results even if the
# operator's terminal closes mid-run.
#
# Usage:
#   bash deploy.sh /path/to/deploy.conf
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
REPORT="/tmp/deploy-$TAG-report.txt"
exec > >(tee -a "$LOG") 2>&1

# Write early-fail context to the report file so Hermes can find something
# even if we exit before Phase 7.
write_report() {
    {
        echo "TAG=$TAG"
        echo "HOST=$(hostname)"
        echo "TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "APP_DIR=$APP_DIR"
        echo "APP_USER=$APP_USER"
        echo "LOG=$LOG"
        echo "STATUS=$1"
        [[ -n "${2:-}" ]] && echo "REASON=$2"
    } > "$REPORT"
}

on_fail() {
    local reason="$1"
    write_report "FAIL" "$reason"
    # Capture last 30 lines of install.sh log if we got that far
    if [[ -n "${INSTALL_LOG:-}" && -f "$INSTALL_LOG" ]]; then
        echo "── last 30 lines of install log ──" >> "$REPORT"
        tail -30 "$INSTALL_LOG" >> "$REPORT"
    fi
    echo "FAIL: $reason  (report: $REPORT)"
    exit 1
}
trap 'on_fail "unexpected error at line $LINENO"' ERR

write_report "IN_PROGRESS"

# ── Phase 0: non-interactive sudo preflight ─────────────────────────────────
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

See docs/DEPLOY_NONINTERACTIVE.md for full setup.

Details:
  hostname:  $(hostname)
  user:      $USER (uid=$EUID)
  tag:       $TAG
  conf:      $CONF
  report:    $REPORT
EOF
        write_report "FAIL" "sudo-n-unavailable"
        exit 1
    fi
    SUDO="sudo -n"
else
    SUDO=""
fi

export DEBIAN_FRONTEND=noninteractive

# ── Phase 1: verify tarball (existence + sha256 if provided) ─────────────────
[[ -f "$TARBALL_PATH" ]] || on_fail "tarball not found at $TARBALL_PATH"
if [[ -n "${TARBALL_SHA256:-}" ]]; then
    actual=$(sha256sum "$TARBALL_PATH" | awk '{print $1}')
    if [[ "$actual" != "$TARBALL_SHA256" ]]; then
        on_fail "tarball sha256 mismatch (expected=$TARBALL_SHA256 actual=$actual)"
    fi
    echo "[deploy] tarball sha256 verified"
else
    echo "[deploy] WARN: TARBALL_SHA256 not set in config; skipping integrity check"
fi

# ── Phase 2: port availability (fail fast if 80/443 are held) ───────────────
for port in 80 443; do
    if ss -ltn "sport = :$port" 2>/dev/null | tail -n +2 | grep -q LISTEN; then
        holder=$(ss -ltnp "sport = :$port" 2>/dev/null | tail -n +2 | head -1)
        on_fail "port $port already bound before install (holder: $holder)"
    fi
done
echo "[deploy] ports 80/443 available"

# ── Phase 3: extract ─────────────────────────────────────────────────────────
STAGE="/tmp/deploy-$TAG-$$-stage"
mkdir -p "$STAGE"
tar -xzf "$TARBALL_PATH" -C "$STAGE"
EXTRACTED="$STAGE/antigravity-predictor-bare-metal-$TAG"
[[ -d "$EXTRACTED" ]] || on_fail "extraction did not produce $EXTRACTED"

# ── Phase 4: install (delegates to install.sh) ──────────────────────────────
INSTALL_LOG="/tmp/install-$TAG-$$.log"
if ! $SUDO env ENABLE_BASIC_AUTH="${ENABLE_BASIC_AUTH:-true}" \
    bash "$EXTRACTED/deploy/bare-metal/install.sh" \
    --app-dir "$APP_DIR" --user "$APP_USER" > >(tee "$INSTALL_LOG") 2>&1; then
    on_fail "install.sh exited non-zero"
fi

# Extract basic-auth password from install.sh's log line — new parseable
# format from beta-1.10.27: `[BASIC_AUTH] user=<U> password=<P>`.
BASIC_AUTH=$(grep -E '^\[BASIC_AUTH\] user=' "$INSTALL_LOG" | tail -1 || true)

# ── Phase 5: populate .env from optional config vars ─────────────────────────
ENV_FILE="$APP_DIR/.env"
[[ -f "$ENV_FILE" ]] || on_fail "install.sh did not produce $ENV_FILE"
for k in ANTHROPIC_API_KEY AGENT_RELAY_CMD SA_INFERENCE_BACKEND OFFSITE_BACKUP_CMD; do
    val="${!k:-}"
    [[ -z "$val" ]] && continue
    esc=$(printf '%s' "$val" | sed 's/[\/&|]/\\&/g')
    $SUDO sed -i "s|^#*$k=.*|$k=$esc|" "$ENV_FILE"
done
$SUDO chown "$APP_USER:$APP_USER" "$ENV_FILE"
$SUDO chmod 600 "$ENV_FILE"

# ── Phase 6: restart services ────────────────────────────────────────────────
$SUDO systemctl restart predictor agent_relay
if [[ "${SA_INFERENCE_BACKEND:-disabled}" == "enabled" ]]; then
    $SUDO systemctl restart signal_agent || echo "WARN: signal_agent restart failed"
fi

# ── Phase 7: verification (delegates to verify.sh for the full 7-check pass) ─
VERIFY_OUT=$($SUDO bash "$EXTRACTED/deploy/bare-metal/verify.sh" "$CONF" 2>&1 || true)
VERIFY_FAILS=$(echo "$VERIFY_OUT" | grep -c '^\s*\[FAIL\]' || echo "0")

# ── Phase 8: report ──────────────────────────────────────────────────────────
{
    echo "TAG=$TAG"
    echo "HOST=$(hostname)"
    echo "TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "APP_DIR=$APP_DIR"
    echo "APP_USER=$APP_USER"
    echo "LOG=$LOG"
    echo "INSTALL_LOG=$INSTALL_LOG"
    [[ -n "$BASIC_AUTH" ]] && echo "$BASIC_AUTH"
    echo "VERIFY_FAILS=$VERIFY_FAILS"
    echo "STATUS=$([[ $VERIFY_FAILS -eq 0 ]] && echo deploy-ok || echo deploy-partial)"
    echo "── verify output ──"
    echo "$VERIFY_OUT"
} > "$REPORT"

echo "─────────────────────────────────────────────────────────"
echo "[STATUS] $([[ $VERIFY_FAILS -eq 0 ]] && echo deploy-ok || echo deploy-partial)"
echo "[TAG] $TAG"
echo "[HOST] $(hostname)"
echo "[APP_DIR] $APP_DIR"
echo "[REPORT] $REPORT"
echo "[LOG] $LOG"
[[ -n "$BASIC_AUTH" ]] && echo "$BASIC_AUTH"
echo "[VERIFY] $VERIFY_FAILS failed checks (see $REPORT for details)"
echo "─────────────────────────────────────────────────────────"

trap - ERR
rm -rf "$STAGE" 2>/dev/null || true

[[ $VERIFY_FAILS -eq 0 ]] || exit 1
