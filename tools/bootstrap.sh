#!/usr/bin/env bash
# ==============================================================================
# Antigravity Predictor — bootstrap
#
# No root. No sudo. No apt. No /opt. No systemd.
# Everything lives under one directory you choose. Delete it to uninstall.
#
# Only requirement: python3 on PATH. Everything else it handles itself.
#
# Usage:
#   ./bootstrap.sh <tarball>            # unpack + install into ./antigravity-predictor
#   APP_DIR=~/foo ./bootstrap.sh <tar>  # or choose where
#   ./bootstrap.sh                      # already extracted; run from inside the dir
# ==============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-$PWD/antigravity-predictor}"
TARBALL="${1:-}"
EXPECT_MODEL_ROWS="${EXPECT_MODEL_ROWS:-49053}"

log()  { printf '\n[bootstrap] %s\n' "$*"; }
warn() { printf '[bootstrap] WARN: %s\n' "$*"; }
fail() { printf '\n[bootstrap] FATAL: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 not on PATH. That is the one thing this needs."
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
log "python3 ${PYVER} at $(command -v python3)"

# Root is USED IF PRESENT, never REQUIRED. Every step below has a user-space
# path. Elevation only ever makes things faster or tidier, never possible.
if   [[ $EUID -eq 0 ]];            then SUDO="";     PRIV="root"
elif sudo -n true 2>/dev/null;     then SUDO="sudo"; PRIV="passwordless sudo"
else                                    SUDO="";     PRIV="unprivileged"
fi
log "privileges: ${PRIV}"

# Opportunistic: if we can install packages, front-load the ones that make
# life easier. Failure here is not an error — the fallbacks below cover it.
if [[ -n "$SUDO" || $EUID -eq 0 ]]; then
    export DEBIAN_FRONTEND=noninteractive
    $SUDO apt-get update -qq 2>/dev/null || true
    $SUDO apt-get install -y -qq python3-venv python3-pip libgomp1 curl ca-certificates \
        2>/dev/null || warn "opportunistic apt install failed; continuing user-space"
fi

# ------------------------------------------------------------------------------
# 1. UNPACK
# ------------------------------------------------------------------------------
if [[ -n "$TARBALL" ]]; then
    [[ -f "$TARBALL" ]] || fail "tarball not found: $TARBALL"
    if [[ -f "${TARBALL}.sha256" ]]; then
        ( cd "$(dirname "$TARBALL")" && sha256sum -c "$(basename "$TARBALL").sha256" >/dev/null ) \
            && log "checksum OK" \
            || fail "checksum mismatch — re-transfer, do not extract"
    else
        warn "no .sha256 sidecar; integrity unverified"
    fi
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    tar xzf "$TARBALL" -C "$TMP"
    SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)"
    mkdir -p "$APP_DIR"
    cp -a "$SRC"/. "$APP_DIR"/
    log "unpacked to ${APP_DIR}"
fi

cd "$APP_DIR" 2>/dev/null || fail "no such directory: $APP_DIR"
[[ -f run.sh ]] || fail "run.sh not in $APP_DIR — wrong directory, or bad extract"

# ------------------------------------------------------------------------------
# 2. VIRTUALENV — three fallbacks, because python3-venv is often absent
#    and we cannot apt-install it.
# ------------------------------------------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
    log "creating virtualenv"
    if python3 -m venv .venv 2>/dev/null; then
        log "  via python3 -m venv"
    elif python3 -m virtualenv .venv 2>/dev/null; then
        log "  via python3 -m virtualenv"
    else
        warn "stdlib venv unavailable (python3-venv not installed); fetching virtualenv zipapp"
        curl -sSfL -o "$PWD/.virtualenv.pyz" https://bootstrap.pypa.io/virtualenv.pyz \
            || fail "no venv module and could not fetch virtualenv.pyz — check network"
        python3 "$PWD/.virtualenv.pyz" .venv || fail "virtualenv.pyz could not create .venv"
        rm -f "$PWD/.virtualenv.pyz"
        log "  via virtualenv.pyz (no root needed)"
    fi
fi
PY=".venv/bin/python"
[[ -x "$PY" ]] || fail "virtualenv exists but has no interpreter"

# ------------------------------------------------------------------------------
# 3. DEPENDENCIES
# ------------------------------------------------------------------------------
log "installing dependencies (1-3 min)"
"$PY" -m pip install --upgrade pip -q 2>/dev/null || warn "pip self-upgrade skipped"
"$PY" -m pip install -q -r requirements.txt || fail "dependency install failed (see output above)"

# --- OpenMP runtime, without root -------------------------------------------
# lib_lightgbm.so links against system libgomp.so.1 and the lightgbm wheel does
# NOT vendor it (verified: no lightgbm.libs/). On a clean OS that import fails.
# scikit-learn's wheel DOES vendor a copy, under a mangled SONAME so it cannot
# satisfy the dependency directly. Symlinking it under the canonical name into
# a venv-local dir on LD_LIBRARY_PATH fixes it with no privileges at all.
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
LIBDIR="$PWD/.venv/oslibs"
if ! ldconfig -p 2>/dev/null | grep -q 'libgomp\.so\.1'; then
    VENDORED="$(find "$SITE" -name 'libgomp*.so.1*' 2>/dev/null | head -1)"
    if [[ -n "$VENDORED" ]]; then
        mkdir -p "$LIBDIR"
        ln -sf "$VENDORED" "$LIBDIR/libgomp.so.1"
        log "system libgomp absent — linked vendored copy into ${LIBDIR}"
    else
        warn "no system or vendored libgomp found; lightgbm import may fail"
    fi
fi
[[ -d "$LIBDIR" ]] && export LD_LIBRARY_PATH="${LIBDIR}:${LD_LIBRARY_PATH:-}"

log "verifying imports"
"$PY" - <<'PY' || fail "dependency verification failed (see above)"
import importlib.util, sys
missing = [m for m in ("lightgbm","fastapi","uvicorn","pandas","numpy",
                       "sklearn","pyarrow","ccxt","loguru","dotenv")
           if importlib.util.find_spec(m) is None]
if missing:
    print("  MISSING:", ", ".join(missing)); sys.exit(1)
import sklearn          # loads vendored OpenMP first
import lightgbm
print(f"  ok  lightgbm {lightgbm.__version__}")
PY

# Persist LD_LIBRARY_PATH for run.sh, so the fix survives this script exiting.
if [[ -d "$LIBDIR" ]]; then
    grep -q 'oslibs' .venv/bin/activate 2>/dev/null || \
        echo "export LD_LIBRARY_PATH=\"${LIBDIR}:\${LD_LIBRARY_PATH:-}\"" >> .venv/bin/activate
    log "LD_LIBRARY_PATH persisted into .venv/bin/activate"
fi

# ------------------------------------------------------------------------------
# 4. ARTIFACT CHECK — guards against the retracted 998-row boosters
# ------------------------------------------------------------------------------
log "verifying models"
ERR=0
for a in btc eth sol; do for s in long short; do
    f="models/model_${a}_${s}.txt"
    if [[ ! -f "$f" ]]; then echo "  MISSING $f"; ERR=1; continue; fi
    rows="$(grep -m1 -o 'internal_count=[0-9]*' "$f" | cut -d= -f2)"
    if [[ "$rows" != "$EXPECT_MODEL_ROWS" ]]
        then echo "  WRONG   $f (rows=$rows, expected $EXPECT_MODEL_ROWS)"; ERR=1
        else echo "  ok      $f"
    fi
done; done
[[ $ERR -eq 0 ]] || fail "model check failed — wrong tarball"

# ------------------------------------------------------------------------------
# 5. CONNECTIVITY — warn only. The dashboard still runs; it just won't tick.
# ------------------------------------------------------------------------------
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://api.bybit.com/v5/market/time 2>/dev/null || echo 000)"
[[ "$CODE" == "200" ]] && log "api.bybit.com reachable" \
                       || warn "api.bybit.com returned '$CODE' — no live market data until this works"

# ------------------------------------------------------------------------------
# 6. REPORT
# ------------------------------------------------------------------------------
mkdir -p reports
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="reports/bootstrap_$(hostname)_${STAMP}.json"
. /etc/os-release 2>/dev/null || true
cat > "$OUT" <<JSON
{
  "timestamp_utc": "${STAMP}",
  "hostname": "$(hostname)",
  "os": "${PRETTY_NAME:-unknown}",
  "python_version": "${PYVER}",
  "app_dir": "${APP_DIR}",
  "venv_ready": true,
  "imports_ok": true,
  "models_verified": true,
  "bybit_http_code": "${CODE}",
  "ran_as_root": $([[ $EUID -eq 0 ]] && echo true || echo false),
  "status": "READY"
}
JSON

log "READY  →  ${OUT}"
cat <<EOS

  Start:     cd ${APP_DIR} && ./run.sh
  Detached:  cd ${APP_DIR} && mkdir -p logs && nohup ./run.sh > logs/run.out 2>&1 & echo \$! > run.pid
  Check:     curl -s localhost:18910/api/status | head -20
  Uninstall: rm -rf ${APP_DIR}

EOS
