#!/usr/bin/env bash
# =============================================================================
# retrain_all.sh — Full model retraining pipeline for the Antigravity Predictor
#
# Flow:
#   1. Download fresh OHLCV from Bybit (BTC/ETH/SOL, 1m/5m/15m/1h/4h/1d)
#   2. Fetch macro data (Gold/Oil/DXY/SPX/VIX) from Yahoo Finance
#   3. Build labeled datasets for each pair (BTC/ETH/SOL)
#   4. Train long + short models for each pair (6 total)
#   5. Evaluate — gate on AUC >= baseline before promoting
#   6. Deploy: copy passing models to models/, backup old ones
#
# Usage:
#   cd /opt/predictor
#   bash retrain_all.sh [--dry-run] [--skip-download] [--skip-macro] [--min-auc 0.60]
#
# Cron (weekly, Sunday 02:00):
#   0 2 * * 0 cd /opt/predictor && bash retrain_all.sh >> logs/retrain.log 2>&1
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"
DATA_DIR="$SCRIPT_DIR/data"
MODEL_DIR="$SCRIPT_DIR/models"
LOG_DIR="$SCRIPT_DIR/logs"
PYTHON="${PYTHON:-python3}"
EXCHANGE="bybit"
MARKET_TYPE="swap"
TIMEFRAME_PRIMARY="15m"
SINCE="2024-01-01T00:00:00Z"   # Start of training window

# Pairs
PAIRS=("BTC/USDT" "ETH/USDT" "SOL/USDT")
PAIR_KEYS=("btc"   "eth"      "sol")

# Model quality gate (retrain is rejected if AUC drops below this)
# Must match .env.example (SA_MIN_AUC=0.54) and docker-compose.yml MIN_AUC default.
MIN_AUC="${MIN_AUC:-0.54}"

# Flags
DRY_RUN=0
SKIP_DOWNLOAD=0
SKIP_MACRO=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)        DRY_RUN=1 ;;
        --skip-download)  SKIP_DOWNLOAD=1 ;;
        --skip-macro)     SKIP_MACRO=1 ;;
        --min-auc=*)      MIN_AUC="${1#*=}" ;;
        --min-auc)        MIN_AUC="$2"; shift ;;
    esac
    shift
done

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
die() { log "ERROR: $*"; exit 1; }

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        log "[DRY-RUN] $*"
    else
        "$@"
    fi
}

check_auc() {
    local metrics_file="$1"
    local label="$2"
    if [[ ! -f "$metrics_file" ]]; then
        log "WARN: metrics file not found: $metrics_file — skipping AUC gate."
        return 0
    fi
    local auc
    auc=$($PYTHON -c "
import json, sys
m = json.load(open('$metrics_file'))
auc = m.get('test_auc') or m.get('val_auc') or 0.0
print(f'{auc:.4f}')
sys.exit(0 if auc >= $MIN_AUC else 1)
" 2>/dev/null) || { log "GATE FAIL [$label]: AUC $auc < $MIN_AUC — model rejected."; return 1; }
    log "GATE PASS [$label]: AUC $auc >= $MIN_AUC"
    return 0
}

# ── Setup ─────────────────────────────────────────────────────────────────────
mkdir -p "$DATA_DIR/raw" "$DATA_DIR/macro" "$DATA_DIR/datasets" \
         "$MODEL_DIR/staging" "$LOG_DIR"

TS=$(date -u '+%Y%m%d_%H%M%S')
log "===== Retrain started at $TS ====="
[[ $DRY_RUN -eq 1 ]] && log "DRY-RUN mode — no files will be written."

cd "$SRC_DIR"

# ── Step 1: Download OHLCV from Bybit ─────────────────────────────────────────
if [[ $SKIP_DOWNLOAD -eq 0 ]]; then
    log "--- Step 1: Downloading OHLCV ---"
    for i in "${!PAIRS[@]}"; do
        PAIR="${PAIRS[$i]}"
        KEY="${PAIR_KEYS[$i]}"
        log "  Fetching $PAIR"

        for TF in 1m 5m 15m 1h 4h 1d; do
            OUT="$DATA_DIR/raw/${KEY}_${TF}.parquet"
            log "    $TF → $OUT"
            run $PYTHON download_ohlcv.py \
                --exchange-id "$EXCHANGE" \
                --symbol "$PAIR" \
                --timeframe "$TF" \
                --since "$SINCE" \
                --market-type "$MARKET_TYPE" \
                --limit 1000 \
                --max-rows 500000 \
                --output-parquet "$OUT"
        done

        # Mark price and funding rate (futures-specific)
        log "    mark price → $DATA_DIR/raw/${KEY}_mark.parquet"
        run $PYTHON download_ohlcv.py \
            --exchange-id "$EXCHANGE" \
            --symbol "$PAIR" \
            --timeframe "$TIMEFRAME_PRIMARY" \
            --since "$SINCE" \
            --market-type "mark_price" \
            --limit 1000 \
            --max-rows 500000 \
            --output-parquet "$DATA_DIR/raw/${KEY}_mark.parquet" || \
            log "    WARN: mark price download failed for $PAIR (non-fatal)"

        log "    funding rate → $DATA_DIR/raw/${KEY}_funding.parquet"
        run $PYTHON download_ohlcv.py \
            --exchange-id "$EXCHANGE" \
            --symbol "$PAIR" \
            --timeframe "1h" \
            --since "$SINCE" \
            --market-type "funding_rate" \
            --limit 1000 \
            --max-rows 500000 \
            --output-parquet "$DATA_DIR/raw/${KEY}_funding.parquet" || \
            log "    WARN: funding rate download failed for $PAIR (non-fatal)"
    done
else
    log "--- Step 1: SKIPPED (--skip-download) ---"
fi

# ── Step 2: Fetch macro data ───────────────────────────────────────────────────
if [[ $SKIP_MACRO -eq 0 ]]; then
    log "--- Step 2: Fetching macro data (Gold/Oil/DXY/SPX/VIX) ---"
    run $PYTHON fetch_macro.py \
        --data-dir "$DATA_DIR/macro" \
        --days 730
else
    log "--- Step 2: SKIPPED (--skip-macro) ---"
fi

# ── Step 3: Build labeled datasets ────────────────────────────────────────────
log "--- Step 3: Building datasets ---"

# BTC primary (ETH + SOL as context)
log "  Building BTC dataset…"
run $PYTHON prepare_full_dataset.py \
    --primary           BTC \
    --primary-candles   "$DATA_DIR/raw/btc_15m.parquet" \
    --primary-mark      "$DATA_DIR/raw/btc_mark.parquet" \
    --primary-funding   "$DATA_DIR/raw/btc_funding.parquet" \
    --primary-1m        "$DATA_DIR/raw/btc_1m.parquet" \
    --primary-5m        "$DATA_DIR/raw/btc_5m.parquet" \
    --primary-1h        "$DATA_DIR/raw/btc_1h.parquet" \
    --primary-4h        "$DATA_DIR/raw/btc_4h.parquet" \
    --primary-1d        "$DATA_DIR/raw/btc_1d.parquet" \
    --ctx-a-candles     "$DATA_DIR/raw/eth_15m.parquet" \
    --ctx-a-mark        "$DATA_DIR/raw/eth_mark.parquet" \
    --ctx-a-funding     "$DATA_DIR/raw/eth_funding.parquet" \
    --ctx-b-candles     "$DATA_DIR/raw/sol_15m.parquet" \
    --ctx-b-mark        "$DATA_DIR/raw/sol_mark.parquet" \
    --ctx-b-funding     "$DATA_DIR/raw/sol_funding.parquet" \
    --macro-dir         "$DATA_DIR/macro" \
    --output            "$DATA_DIR/datasets/btc_full.parquet" \
    --tp-atr-mult 1.5 --sl-atr-mult 1.0 --horizon-bars 4

# ETH primary (BTC + SOL as context)
log "  Building ETH dataset…"
run $PYTHON prepare_full_dataset.py \
    --primary           ETH \
    --primary-candles   "$DATA_DIR/raw/eth_15m.parquet" \
    --primary-mark      "$DATA_DIR/raw/eth_mark.parquet" \
    --primary-funding   "$DATA_DIR/raw/eth_funding.parquet" \
    --primary-1m        "$DATA_DIR/raw/eth_1m.parquet" \
    --primary-5m        "$DATA_DIR/raw/eth_5m.parquet" \
    --primary-1h        "$DATA_DIR/raw/eth_1h.parquet" \
    --primary-4h        "$DATA_DIR/raw/eth_4h.parquet" \
    --primary-1d        "$DATA_DIR/raw/eth_1d.parquet" \
    --ctx-a-candles     "$DATA_DIR/raw/btc_15m.parquet" \
    --ctx-a-mark        "$DATA_DIR/raw/btc_mark.parquet" \
    --ctx-a-funding     "$DATA_DIR/raw/btc_funding.parquet" \
    --ctx-b-candles     "$DATA_DIR/raw/sol_15m.parquet" \
    --ctx-b-mark        "$DATA_DIR/raw/sol_mark.parquet" \
    --ctx-b-funding     "$DATA_DIR/raw/sol_funding.parquet" \
    --macro-dir         "$DATA_DIR/macro" \
    --output            "$DATA_DIR/datasets/eth_full.parquet" \
    --tp-atr-mult 1.5 --sl-atr-mult 1.0 --horizon-bars 4

# SOL primary (BTC + ETH as context)
log "  Building SOL dataset…"
run $PYTHON prepare_full_dataset.py \
    --primary           SOL \
    --primary-candles   "$DATA_DIR/raw/sol_15m.parquet" \
    --primary-mark      "$DATA_DIR/raw/sol_mark.parquet" \
    --primary-funding   "$DATA_DIR/raw/sol_funding.parquet" \
    --primary-1m        "$DATA_DIR/raw/sol_1m.parquet" \
    --primary-5m        "$DATA_DIR/raw/sol_5m.parquet" \
    --primary-1h        "$DATA_DIR/raw/sol_1h.parquet" \
    --primary-4h        "$DATA_DIR/raw/sol_4h.parquet" \
    --primary-1d        "$DATA_DIR/raw/sol_1d.parquet" \
    --ctx-a-candles     "$DATA_DIR/raw/btc_15m.parquet" \
    --ctx-a-mark        "$DATA_DIR/raw/btc_mark.parquet" \
    --ctx-a-funding     "$DATA_DIR/raw/btc_funding.parquet" \
    --ctx-b-candles     "$DATA_DIR/raw/eth_15m.parquet" \
    --ctx-b-mark        "$DATA_DIR/raw/eth_mark.parquet" \
    --ctx-b-funding     "$DATA_DIR/raw/eth_funding.parquet" \
    --macro-dir         "$DATA_DIR/macro" \
    --output            "$DATA_DIR/datasets/sol_full.parquet" \
    --tp-atr-mult 1.5 --sl-atr-mult 1.0 --horizon-bars 4

# ── Step 4 & 5: Train, evaluate, gate ─────────────────────────────────────────
log "--- Steps 4-5: Training + evaluation ---"

FAILED_MODELS=()
PASSED_MODELS=()

for KEY in btc eth sol; do
    DATASET="$DATA_DIR/datasets/${KEY}_full.parquet"
    if [[ ! -f "$DATASET" ]]; then
        log "WARN: dataset missing for $KEY — skipping."
        continue
    fi

    for SIDE in long short; do
        if [[ "$SIDE" == "long" ]]; then
            LABEL="label_tp_before_sl_1h"
        else
            LABEL="label_short_tp_before_sl_1h"
        fi
        OUT_DIR="$MODEL_DIR/staging/${KEY}_${SIDE}"
        MODEL_KEY="${KEY}_${SIDE}"

        log "  Training $MODEL_KEY…"
        run $PYTHON train_lightgbm.py \
            --dataset     "$DATASET" \
            --output-dir  "$OUT_DIR" \
            --label-col   "$LABEL"

        # AUC gate
        METRICS_FILE="$OUT_DIR/metrics.json"
        if check_auc "$METRICS_FILE" "$MODEL_KEY"; then
            PASSED_MODELS+=("$MODEL_KEY")
        else
            FAILED_MODELS+=("$MODEL_KEY")
        fi
    done
done

# ── Step 6: Deploy passing models ─────────────────────────────────────────────
log "--- Step 6: Deploying models ---"

if [[ ${#PASSED_MODELS[@]} -eq 0 ]]; then
    die "All models failed AUC gate. No deployment."
fi

# Backup existing models
BACKUP_DIR="$MODEL_DIR/backup_${TS}"
mkdir -p "$BACKUP_DIR"
for f in "$MODEL_DIR"/*.txt; do
    [[ -f "$f" ]] && cp "$f" "$BACKUP_DIR/" && log "  Backed up $(basename $f)"
done

# Deploy passing models
for MODEL_KEY in "${PASSED_MODELS[@]}"; do
    SRC_MODEL="$MODEL_DIR/staging/${MODEL_KEY}/model/model.txt"
    DST_MODEL="$MODEL_DIR/model_${MODEL_KEY}.txt"
    if [[ -f "$SRC_MODEL" ]]; then
        run cp "$SRC_MODEL" "$DST_MODEL"
        log "  Deployed: $DST_MODEL"
    else
        log "  WARN: staging model not found: $SRC_MODEL"
    fi
done

# Report skipped/failed
if [[ ${#FAILED_MODELS[@]} -gt 0 ]]; then
    log "WARN: These models failed the AUC gate and were NOT deployed:"
    for m in "${FAILED_MODELS[@]}"; do log "    - $m"; done
    log "Previous versions remain active for failed models."
fi

# ── Reload predictor server ────────────────────────────────────────────────────
log "--- Reloading predictor service ---"
if [[ -f /.dockerenv ]]; then
    # Running inside the retrain Docker container.
    # The predictor is a separate container — we cannot restart it from here.
    # Models are on a shared bind-mount: the predictor will see new files on
    # next restart. Signal the operator via a sentinel file.
    SENTINEL="${MODEL_DIR}/.retrain_complete"
    echo "$TS" > "$SENTINEL"
    log "  [Docker] Wrote sentinel: $SENTINEL"
    log "  [Docker] Run: docker compose restart predictor"
    log "  [Docker] Or the predictor will pick up new models on next container restart."
elif systemctl is-active --quiet predictor 2>/dev/null; then
    # Bare-metal systemd path.
    run systemctl restart predictor
    log "  predictor.service restarted."
else
    log "  predictor.service not running — skipping restart (start it manually)."
fi

log "===== Retrain complete: ${#PASSED_MODELS[@]} deployed, ${#FAILED_MODELS[@]} rejected ====="
