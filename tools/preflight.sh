#!/usr/bin/env bash
# =============================================================================
# tools/preflight.sh — Deploy Readiness Preflight Check Harness
#
# Emits structured preflight report to reports/preflight_{host}_{ts}.json.
# Read-only, no mutation, exit code = number of failed blocker checks.
# Checks: P-01, P-03, P-04, P-05, P-06, P-07, P-08, P-12.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS_DIR="${REPO_ROOT}/reports"
mkdir -p "${REPORTS_DIR}"

HOST="$(hostname -s 2>/dev/null || hostname)"
TS="$(date -u +"%Y%m%dT%H%M%SZ")"
REPORT_FILE="${REPORTS_DIR}/preflight_${HOST}_${TS}.json"

BLOCKER_COUNT=0

P01_STATUS="PASS"; P01_DETAILS=""
P03_STATUS="PASS"; P03_DETAILS=""
P04_STATUS="PASS"; P04_DETAILS=""
P05_STATUS="PASS"; P05_DETAILS=""
P06_STATUS="PASS"; P06_DETAILS=""
P07_STATUS="PASS"; P07_DETAILS=""
P08_STATUS="PASS"; P08_DETAILS=""
P12_STATUS="PASS"; P12_DETAILS=""

# P-01: User & Sudo Rights Check
if [[ $EUID -eq 0 ]]; then
    P01_DETAILS="Running as root"
elif sudo -n true 2>/dev/null; then
    P01_DETAILS="Passwordless sudo verified"
else
    P01_STATUS="FAIL"
    P01_DETAILS="Passwordless sudo not available for user $(whoami)"
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# P-03: Disk Space Check (Require >= 2.0 GB free on target partition)
FREE_KB=$(df -k "${REPO_ROOT}" | tail -n 1 | awk '{print $4}')
FREE_GB=$(echo "scale=2; ${FREE_KB} / 1048576" | bc 2>/dev/null || awk "BEGIN {print ${FREE_KB}/1048576}")
if (( $(echo "${FREE_GB} >= 2.0" | bc -l 2>/dev/null || awk "BEGIN {print (${FREE_GB}>=2.0)?1:0}") )); then
    P03_DETAILS="${FREE_GB} GB free space available (>= 2.0 GB required)"
else
    P03_STATUS="FAIL"
    P03_DETAILS="Insufficient disk space: ${FREE_GB} GB free (< 2.0 GB required)"
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# P-04: Python Environment Check (Python 3.10+ required)
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    PY_MAJOR=$(echo "${PY_VER}" | cut -d. -f1)
    PY_MINOR=$(echo "${PY_VER}" | cut -d. -f2)
    if [[ "${PY_MAJOR}" -ge 3 && "${PY_MINOR}" -ge 10 ]]; then
        P04_DETAILS="Python ${PY_VER} detected (>= 3.10 required)"
    else
        P04_STATUS="FAIL"
        P04_DETAILS="Python version ${PY_VER} unsupported (< 3.10)"
        BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
    fi
else
    P04_STATUS="FAIL"
    P04_DETAILS="python3 executable not found"
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# P-05: Port Availability / Loopback Binding Check (:18910)
if ss -ltn 'sport = :18910' 2>/dev/null | grep -q ':18910'; then
    P05_DETAILS="Port 18910 active/bound on system"
else
    P05_DETAILS="Port 18910 available"
fi

# P-06: Directory Permissions Check (Check write/search access to REPO_ROOT)
TARGET_PERM_DIR="${TEST_DIR:-${REPO_ROOT}/.perm_test_dir}"
if mkdir -p "${TARGET_PERM_DIR}" 2>/dev/null && rmdir "${TARGET_PERM_DIR}" 2>/dev/null; then
    P06_DETAILS="Target directory permissions writeable/valid (${REPO_ROOT})"
else
    P06_STATUS="FAIL"
    P06_DETAILS="Permission denied accessing target directory (${TARGET_PERM_DIR})"
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# P-07: Systemd Availability Check
if command -v systemctl >/dev/null 2>&1 && systemctl status >/dev/null 2>&1; then
    P07_DETAILS="Systemd systemctl active and responsive"
else
    P07_STATUS="FAIL"
    P07_DETAILS="Systemd systemctl unavailable or non-functional"
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# P-08: Network Connectivity Check (ping/HTTP to api.bybit.com or 127.0.0.1)
if curl -sf --max-time 5 https://api.bybit.com/v5/market/time >/dev/null 2>&1 || curl -sf --max-time 3 http://127.0.0.1:18910/api/status >/dev/null 2>&1; then
    P08_DETAILS="Network connectivity to market API / predictor verified"
else
    P08_STATUS="FAIL"
    P08_DETAILS="Network connectivity check failed (api.bybit.com & 127.0.0.1)"
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# P-12: Model Booster Files Presence Check (6 model files in models/)
MISSING_MODELS=()
for m in model_btc_long.txt model_btc_short.txt model_eth_long.txt model_eth_short.txt model_sol_long.txt model_sol_short.txt; do
    if [[ ! -f "${REPO_ROOT}/models/${m}" ]]; then
        MISSING_MODELS+=("${m}")
    fi
done

if [[ ${#MISSING_MODELS[@]} -eq 0 ]]; then
    P12_DETAILS="All 6 production model boosters present in models/"
else
    P12_STATUS="FAIL"
    P12_DETAILS="Missing ${#MISSING_MODELS[@]} model booster files in models/: ${MISSING_MODELS[*]}"
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# Format JSON output
cat <<EOF > "${REPORT_FILE}"
{
  "host": "${HOST}",
  "timestamp": "${TS}",
  "read_only": true,
  "checks": {
    "P-01_sudo_permissions": {
      "status": "${P01_STATUS}",
      "details": "${P01_DETAILS}"
    },
    "P-03_disk_space": {
      "status": "${P03_STATUS}",
      "details": "${P03_DETAILS}"
    },
    "P-04_python_env": {
      "status": "${P04_STATUS}",
      "details": "${P04_DETAILS}"
    },
    "P-05_port_availability": {
      "status": "${P05_STATUS}",
      "details": "${P05_DETAILS}"
    },
    "P-06_directory_permissions": {
      "status": "${P06_STATUS}",
      "details": "${P06_DETAILS}"
    },
    "P-07_systemd_availability": {
      "status": "${P07_STATUS}",
      "details": "${P07_DETAILS}"
    },
    "P-08_network_connectivity": {
      "status": "${P08_STATUS}",
      "details": "${P08_DETAILS}"
    },
    "P-12_model_presence": {
      "status": "${P12_STATUS}",
      "details": "${P12_DETAILS}"
    }
  },
  "blocker_count": ${BLOCKER_COUNT}
}
EOF

chmod 644 "${REPORT_FILE}"
echo "[preflight] Report saved to ${REPORT_FILE}"
echo "[preflight] Blocker count: ${BLOCKER_COUNT}"

exit ${BLOCKER_COUNT}
