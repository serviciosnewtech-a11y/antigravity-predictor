#!/usr/bin/env bash
# =============================================================================
# hermes_deploy.sh — one-shot bare-metal deploy for Hermes (or any operator).
#
# Reads config from a .conf file, runs preflight, extracts the tarball,
# invokes install.sh, populates .env, restarts services, verifies, reports.
# On any failure, exits non-zero with a clear reason. No prompts, no retries,
# no interactive branches.
#
# Usage (on the target host, as root):
#   bash hermes_deploy.sh /path/to/deploy.conf
#
# Config file: see hermes_deploy.conf.example. Every required var listed
# there must be set (unset = fail-fast at preflight); optional vars can be
# blank strings.
# =============================================================================
set -uo pipefail

CONF="${1:-}"
[[ -n "$CONF" && -f "$CONF" ]] || {
    echo "ERROR: usage: $0 /path/to/deploy.conf" >&2
    echo "       see hermes_deploy.conf.example for the required format." >&2
    exit 2
}

# shellcheck source=/dev/null
. "$CONF"

# ── Colour-free logger with timestamped lines that grep cleanly ──────────────
ts()   { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log()  { echo "[$(ts)] [hermes_deploy] $*"; }
fail() { echo "[$(ts)] [hermes_deploy] [FAIL] $*" >&2; exit 1; }
warn() { echo "[$(ts)] [hermes_deploy] [WARN] $*" >&2; }

# ── Track warnings for the final report ──────────────────────────────────────
WARNINGS=()
capture_warn() { warn "$*"; WARNINGS+=("$*"); }

# =============================================================================
# Phase 0 — Preflight
# =============================================================================

log "phase 0/4 — preflight"

# 0.0 required config vars present + non-empty
for v in TAG TARBALL_PATH TARBALL_SHA256 APP_DIR APP_USER; do
    [[ -n "${!v:-}" ]] || fail "config missing required var: $v (see hermes_deploy.conf.example)"
done

# 0.1 root
[[ $EUID -eq 0 ]] || fail "not running as root (EUID=$EUID). Hermes must be resident as root for non-interactive deploy."

# 0.2 OS is Debian-family
[[ -f /etc/os-release ]] || fail "/etc/os-release missing; cannot verify OS."
# shellcheck source=/dev/null
. /etc/os-release
case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) : ;;
    *) fail "OS is '${ID:-unknown}' (want Debian-family). install.sh uses apt-get." ;;
esac

# 0.3 disk: at least 4GB free where APP_DIR will live
target_parent=$(dirname "$APP_DIR")
[[ -d "$target_parent" ]] || fail "APP_DIR parent does not exist: $target_parent"
free_kb=$(df -P "$target_parent" | awk 'NR==2 {print $4}')
[[ $free_kb -gt 4000000 ]] || fail "<4GB free on $target_parent (have ${free_kb}KB, need 4000000)"

# 0.4 network + apt reachability
apt-get update -qq 2>/dev/null || fail "'apt-get update' failed. Check network/apt sources."
curl -sfI --max-time 10 https://pypi.org/simple/ >/dev/null || fail "PyPI unreachable; predictor deps install will fail."

# 0.5 baseline packages (front-load python3-venv so bootstrap doesn't crash)
apt-get install -y -qq curl ca-certificates python3 python3-pip python3-venv git tar >/dev/null 2>&1 \
    || fail "baseline apt install failed. Run manually to see output:  apt-get install python3-venv git ..."

# 0.6 ports free (nginx will bind 80/443)
for port in 80 443; do
    if ss -ltn "sport = :$port" 2>/dev/null | tail -n +2 | grep -q LISTEN; then
        fail "port $port already bound. Free it before deploy:  ss -ltnp sport = :$port"
    fi
done

# 0.7 tarball exists + sha256 matches
[[ -f "$TARBALL_PATH" ]] || fail "tarball not found at TARBALL_PATH=$TARBALL_PATH"
actual=$(sha256sum "$TARBALL_PATH" | awk '{print $1}')
[[ "$actual" == "$TARBALL_SHA256" ]] || fail "tarball sha256 mismatch. expected=$TARBALL_SHA256 got=$actual"

log "phase 0/4 — preflight OK"

# =============================================================================
# Phase 1 — Extract + inspect
# =============================================================================

log "phase 1/4 — extract"

STAGE="/tmp/hermes-deploy-$TAG-$$"
mkdir -p "$STAGE"
tar -xzf "$TARBALL_PATH" -C "$STAGE" || fail "tarball extract failed"
EXTRACTED="$STAGE/antigravity-predictor-bare-metal-$TAG"
[[ -d "$EXTRACTED" ]] || fail "tarball did not extract as expected; missing $EXTRACTED"
[[ -x "$EXTRACTED/deploy/bare-metal/install.sh" ]] || fail "install.sh missing or not executable inside tarball"

# Sanity: this tarball's install.sh supports --app-dir/--user (beta-1.10.24+).
# Silently fall back to env-var form if it doesn't (older tarball, still works).
if grep -q 'while \[\[ $# -gt 0 \]\]' "$EXTRACTED/deploy/bare-metal/install.sh"; then
    INSTALL_INVOKE=("bash" "$EXTRACTED/deploy/bare-metal/install.sh" "--app-dir" "$APP_DIR" "--user" "$APP_USER")
else
    capture_warn "install.sh predates beta-1.10.24 flag parsing; falling back to env-var invocation"
    INSTALL_INVOKE=("env" "APP_DIR=$APP_DIR" "APP_USER=$APP_USER" "bash" "$EXTRACTED/deploy/bare-metal/install.sh")
fi

log "phase 1/4 — extracted to $EXTRACTED"

# =============================================================================
# Phase 2 — Deploy (install.sh + .env + service restart)
# =============================================================================

log "phase 2/4 — install"
INSTALL_LOG="/tmp/hermes-install-$TAG-$$.log"
"${INSTALL_INVOKE[@]}" 2>&1 | tee "$INSTALL_LOG"
rc=${PIPESTATUS[0]}
[[ $rc -eq 0 ]] || fail "install.sh exited $rc. Log: $INSTALL_LOG"

# Capture the once-printed basic-auth password from the install log.
# install.sh line: "[INSTALL] Basic auth enabled — user: <U>  password: <P>"
BASIC_AUTH_LINE=$(grep 'Basic auth enabled' "$INSTALL_LOG" || true)
if [[ -n "$BASIC_AUTH_LINE" ]]; then
    BASIC_AUTH_USER=$(sed -n 's/.*user: \([^ ]*\).*/\1/p' <<<"$BASIC_AUTH_LINE")
    BASIC_AUTH_PASS=$(sed -n 's/.*password: \(.*\)/\1/p' <<<"$BASIC_AUTH_LINE")
    [[ -n "$BASIC_AUTH_PASS" ]] || capture_warn "basic auth was enabled but password not parseable from log"
fi

# Populate .env from the config file. sed anchored to line-start with optional
# leading '#' so we replace either the template's commented placeholder or
# an already-set value, and don't accidentally match inside a comment body.
ENV_FILE="$APP_DIR/.env"
[[ -f "$ENV_FILE" ]] || fail ".env template was not written by install.sh at $ENV_FILE"

set_env() {
    local key="$1" val="$2"
    # Escape sed replacement metachars in the value
    local esc
    esc=$(printf '%s' "$val" | sed 's/[\/&|]/\\&/g')
    if grep -qE "^#?$key=" "$ENV_FILE"; then
        sed -i "s|^#*$key=.*|$key=$esc|" "$ENV_FILE"
    else
        echo "$key=$val" >>"$ENV_FILE"
    fi
}

[[ -n "${ANTHROPIC_API_KEY:-}"    ]] && set_env "ANTHROPIC_API_KEY"    "$ANTHROPIC_API_KEY"
[[ -n "${AGENT_RELAY_CMD:-}"      ]] && set_env "AGENT_RELAY_CMD"      "$AGENT_RELAY_CMD"
[[ -n "${SA_INFERENCE_BACKEND:-}" ]] && set_env "SA_INFERENCE_BACKEND" "$SA_INFERENCE_BACKEND"
[[ -n "${OFFSITE_BACKUP_CMD:-}"   ]] && set_env "OFFSITE_BACKUP_CMD"   "$OFFSITE_BACKUP_CMD"

chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Restart services that read .env
systemctl restart predictor    || fail "predictor.service restart failed"
systemctl restart agent_relay  || fail "agent_relay.service restart failed"
if [[ "${SA_INFERENCE_BACKEND:-disabled}" == "enabled" ]]; then
    systemctl restart signal_agent || capture_warn "signal_agent.service restart failed (SA_INFERENCE_BACKEND=enabled but service errored)"
fi

# Optional HTTPS
if [[ -n "${DEPLOY_DOMAIN:-}" ]]; then
    if [[ -z "${LUIS_EMAIL:-}" ]]; then
        capture_warn "DEPLOY_DOMAIN set but LUIS_EMAIL empty; skipping certbot (certbot -m required)"
    else
        certbot --nginx -d "$DEPLOY_DOMAIN" -n --agree-tos -m "$LUIS_EMAIL" \
            || capture_warn "certbot failed for $DEPLOY_DOMAIN; deploy is otherwise fine, HTTPS not enabled"
    fi
fi

log "phase 2/4 — install OK"

# =============================================================================
# Phase 3 — Verify (7 checks; any FAIL exits non-zero)
# =============================================================================

log "phase 3/4 — verify"
VERIFY_RESULTS=()
check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        VERIFY_RESULTS+=("$label: PASS")
    else
        VERIFY_RESULTS+=("$label: FAIL")
        fail "verification check failed: $label"
    fi
}

# 3.1 services active
check "3.1 predictor.service active"   bash -c "systemctl is-active predictor   | grep -q '^active$'"
check "3.1 agent_relay.service active" bash -c "systemctl is-active agent_relay | grep -q '^active$'"

# 3.2 /api/status responds + bound to loopback (not public interface)
STATUS_JSON="/tmp/hermes-status-$TAG-$$.json"
check "3.2 /api/status responds" bash -c "curl -sf --max-time 5 http://127.0.0.1:18910/api/status -o $STATUS_JSON"
check "3.2 predictor bound to loopback" bash -c "ss -ltn 'sport = :18910' | grep -q '127.0.0.1:18910'"

# 3.3 nginx auth wall (if basic auth enabled)
if [[ "${ENABLE_BASIC_AUTH:-true}" == "true" ]]; then
    code=$(curl -so /dev/null -w "%{http_code}" http://127.0.0.1/ 2>/dev/null || echo "000")
    [[ "$code" == "401" ]] || fail "3.3 dashboard NOT behind auth (got $code, expected 401)"
    VERIFY_RESULTS+=("3.3 dashboard auth-walled: PASS")
else
    VERIFY_RESULTS+=("3.3 dashboard auth-walled: SKIPPED (ENABLE_BASIC_AUTH=false)")
fi

# 3.4 firewall configured
check "3.4 ufw allows SSH" bash -c "ufw status 2>/dev/null | grep -qE '22.*ALLOW|OpenSSH.*ALLOW'"
check "3.4 ufw allows 80" bash -c "ufw status 2>/dev/null | grep -q '80.*ALLOW'"

# 3.5 timers scheduled
for t in predictor_backup forge_backup forge_scorecard config_backup sync_offsite macro_refresh; do
    check "3.5 $t.timer scheduled" bash -c "systemctl list-timers --no-pager 2>/dev/null | grep -q ${t}.timer"
done

# 3.6 forge scorecard runs end-to-end
SCORECARD_DUMP=$(dirname "$APP_DIR")/$(basename "$APP_DIR")-forge-scorecard/scorecard.txt
check "3.6 forge_scorecard.py runs" \
    sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/forge_scorecard.py"
check "3.6 scorecard dump non-empty" bash -c "[[ -s '$SCORECARD_DUMP' ]]"

# 3.7 all four backup scripts execute (sync_offsite exits 0 when OFFSITE_BACKUP_CMD unset)
for script in backup_signal_log.py backup_forge_db.py backup_config_and_secrets.py sync_backups_offsite.py; do
    check "3.7 $script runs" \
        sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/$script"
done

BACKUP_DIR=$(dirname "$APP_DIR")/$(basename "$APP_DIR")-backups
[[ -d "$BACKUP_DIR" ]] || capture_warn "backup dir $BACKUP_DIR does not exist post-run"

log "phase 3/4 — verify OK"

# =============================================================================
# Phase 4 — Report (fixed-format for easy parsing)
# =============================================================================

echo
echo "==================== hermes_deploy REPORT ===================="
echo "[STATUS] deploy-ok"
echo "[TAG] $TAG"
echo "[HOST] $(hostname)"
echo "[APP_DIR] $APP_DIR"
echo "[APP_USER] $APP_USER"
if [[ "${ENABLE_BASIC_AUTH:-true}" == "true" ]]; then
    echo "[BASIC_AUTH] enabled  user=${BASIC_AUTH_USER:-?}  password=${BASIC_AUTH_PASS:-?}"
else
    echo "[BASIC_AUTH] disabled"
fi
if [[ -n "${DEPLOY_DOMAIN:-}" ]]; then
    expiry=$(certbot certificates 2>/dev/null | grep -A1 "$DEPLOY_DOMAIN" | grep 'Expiry Date' | head -1 | awk -F: '{print $2}')
    echo "[HTTPS] enabled  domain=$DEPLOY_DOMAIN  expiry=${expiry:-unknown}"
else
    echo "[HTTPS] http-only"
fi
echo "[TIMERS_NEXT_FIRE]"
systemctl list-timers --no-pager 2>/dev/null \
    | awk 'NR==1 || /predictor_backup|forge_backup|forge_scorecard|config_backup|sync_offsite|macro_refresh/' \
    | sed 's/^/  /'
echo "[ENV_SET]"
for k in ANTHROPIC_API_KEY AGENT_RELAY_CMD SA_INFERENCE_BACKEND OFFSITE_BACKUP_CMD; do
    val=$(grep -E "^$k=" "$ENV_FILE" | cut -d= -f2-)
    if [[ -z "$val" ]]; then echo "  $k: empty"; else echo "  $k: set"; fi
done
echo "[VERIFY_SUMMARY]"
for r in "${VERIFY_RESULTS[@]}"; do
    echo "  $r"
done
echo "[WARNINGS]"
if [[ ${#WARNINGS[@]} -eq 0 ]]; then
    echo "  none"
else
    for w in "${WARNINGS[@]}"; do
        echo "  - $w"
    done
fi
echo "[LOGS]"
echo "  install log: $INSTALL_LOG"
echo "  status JSON: $STATUS_JSON"
echo "  scorecard:   $SCORECARD_DUMP"
echo "=============================================================="

# Cleanup staging dir; the extracted tarball copy is redundant now.
rm -rf "$STAGE" 2>/dev/null || true

exit 0
