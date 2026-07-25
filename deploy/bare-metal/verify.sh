#!/usr/bin/env bash
# =============================================================================
# verify.sh — standalone 7-check post-deploy verification.
#
# Same block deploy.sh runs at its minimal-verify phase, expanded to the
# full 7 checks from the original .25 hermes_deploy.sh Phase 3. Runnable
# any time after a deploy to confirm state — no install, no changes.
#
# Usage:
#   bash verify.sh /path/to/deploy.conf
#
# Exit code is number of failed checks (0 = all pass).
# =============================================================================
set -uo pipefail

CONF="${1:-}"
[[ -n "$CONF" && -f "$CONF" ]] || { echo "usage: $0 /path/to/deploy.conf" >&2; exit 2; }
# shellcheck source=/dev/null
. "$CONF"

for v in APP_DIR APP_USER; do
    [[ -n "${!v:-}" ]] || { echo "ERROR: config missing $v" >&2; exit 2; }
done

SUDO=""
if [[ $EUID -ne 0 ]]; then
    if sudo -n true 2>/dev/null; then
        SUDO="sudo -n"
    else
        echo "ERROR: passwordless sudo required (some checks read systemd/ufw)" >&2
        exit 1
    fi
fi

FAILURES=0
pass() { echo "  [PASS] $*"; }
fail() { echo "  [FAIL] $*"; FAILURES=$((FAILURES + 1)); }
skip() { echo "  [SKIP] $*"; }

check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi
}

echo "[verify] running 7-check verification against $APP_DIR"

# 3.1 services active
check "3.1 predictor.service active" \
    bash -c "$SUDO systemctl is-active predictor 2>/dev/null | grep -q '^active$'"
check "3.1 agent_relay.service active" \
    bash -c "$SUDO systemctl is-active agent_relay 2>/dev/null | grep -q '^active$'"

# 3.2 /api/status responds + loopback binding
check "3.2 /api/status responds" \
    curl -sf --max-time 5 http://127.0.0.1:18910/api/status
check "3.2 predictor bound to loopback" \
    bash -c "ss -ltn 'sport = :18910' 2>/dev/null | grep -q '127.0.0.1:18910'"

# 3.3 nginx auth wall
if [[ "${ENABLE_BASIC_AUTH:-true}" == "true" ]]; then
    code=$(curl -so /dev/null -w "%{http_code}" http://127.0.0.1/ 2>/dev/null || echo "000")
    if [[ "$code" == "401" ]]; then
        pass "3.3 dashboard auth-walled (401)"
    else
        fail "3.3 dashboard NOT behind auth (got $code, expected 401)"
    fi
else
    skip "3.3 dashboard auth-walled (ENABLE_BASIC_AUTH=false)"
fi

# 3.4 firewall
check "3.4 ufw allows SSH" \
    bash -c "$SUDO ufw status 2>/dev/null | grep -qE '22.*ALLOW|OpenSSH.*ALLOW'"
check "3.4 ufw allows 80" \
    bash -c "$SUDO ufw status 2>/dev/null | grep -q '80.*ALLOW'"

# 3.5 timers scheduled
for t in predictor_backup forge_backup forge_scorecard config_backup sync_offsite macro_refresh; do
    check "3.5 $t.timer scheduled" \
        bash -c "$SUDO systemctl list-timers --no-pager 2>/dev/null | grep -q ${t}.timer"
done

# 3.6 forge scorecard end-to-end
if $SUDO -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/forge_scorecard.py" >/dev/null 2>&1; then
    pass "3.6 forge_scorecard.py runs"
else
    fail "3.6 forge_scorecard.py errored"
fi
SCORECARD_DUMP="$(dirname "$APP_DIR")/$(basename "$APP_DIR")-forge-scorecard/scorecard.txt"
if [[ -s "$SCORECARD_DUMP" ]]; then
    pass "3.6 scorecard dump non-empty ($SCORECARD_DUMP)"
else
    fail "3.6 scorecard dump missing/empty at $SCORECARD_DUMP"
fi

# 3.7 all four backup scripts
for script in backup_signal_log.py backup_forge_db.py backup_config_and_secrets.py sync_backups_offsite.py; do
    if $SUDO -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/$script" >/dev/null 2>&1; then
        pass "3.7 $script runs"
    else
        fail "3.7 $script errored"
    fi
done

echo "[verify] failed checks: $FAILURES"
exit $FAILURES
