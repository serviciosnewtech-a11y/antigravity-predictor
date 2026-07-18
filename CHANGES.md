# Predictor — Change Log

> **Sync reference for Antigravity and all workers.**
> Every file touched in this session is recorded here with full rationale.
> Status: ✅ Implemented | 📋 Proposed only | ⚠️ Requires review

---

## Session: 2026-07-17

### Context
- Client goal: advice-only advisory system at 15m scalping, moving to execution once validated
- Architecture decision: unify around the Predictor (FastAPI + WS), not the Freqtrade bot
- Retraining goal: add 1m/5m micro-structure + 1h/4h/1d trend validation to existing macro pipeline
- Deployment target: Cloud VPS 6 (6 vCPU / 12GB RAM / 200GB SSD)

---

## New Files Created

### `src/fetch_macro.py` ✅
**Path:** `products/Predictor/src/fetch_macro.py`
**What:** Downloads Gold (GC=F), Oil (CL=F), DXY (DX-Y.NYB), S&P500 (^GSPC), VIX (^VIX) from Yahoo Finance via `yfinance`. Saves daily OHLCV + derived features (log returns, EMA ratio, trend, trend_dir) as parquet to `data/macro/<asset>.parquet`.
**Why:** The July 7 models already include these macro features (confirmed in `models/training_report_20260707.json`). This script keeps that data fresh on a schedule.
**Run:** `python3 src/fetch_macro.py --data-dir data/macro --days 730`
**Cron:** Driven by `macro_refresh.timer` (every 1h)

---

### `src/prepare_full_dataset.py` ✅
**Path:** `products/Predictor/src/prepare_full_dataset.py`
**What:** Full labeled dataset builder. Replaces whatever one-off script produced `data/full_training_dataset.parquet` on July 7. Handles:
- Primary pair 15m candles → base + structure + futures features (existing `lgbm_poc` pipeline)
- **1m / 5m micro-structure** → aggregated to 15m: bull_ratio, vol_tail_pct, max_body_ratio, trend, atr_ratio, volume_zscore (6 features × 2 TFs = 12 new features)
- **1h / 4h / 1d trend validation** → log_return_1/3, EMA fast/slow, trend_strength, trend_dir, atr_pct, volume_zscore, regime (9 features × 3 TFs = 27 features)
- **Cross-asset context** → 2 other pairs' return_1/3, trend, volume_block (4 features × 2 pairs = 8 features)
- **Macro (daily, forward-filled)** → Gold/Oil/DXY/SPX/VIX return_1d/5d, ema_fast/slow, trend, trend_dir (6 features × 5 assets = 30 features)
- **Both long AND short labels** (see labels.py change below)

**Usage:**
```bash
python3 prepare_full_dataset.py \
    --primary BTC \
    --primary-candles data/raw/btc_15m.parquet \
    --primary-1m data/raw/btc_1m.parquet \
    --primary-5m data/raw/btc_5m.parquet \
    --primary-1h data/raw/btc_1h.parquet \
    --primary-4h data/raw/btc_4h.parquet \
    --primary-1d data/raw/btc_1d.parquet \
    --ctx-a-candles data/raw/eth_15m.parquet \
    --ctx-b-candles data/raw/sol_15m.parquet \
    --macro-dir data/macro \
    --output data/datasets/btc_full.parquet
```

---

### `retrain_all.sh` ✅
**Path:** `products/Predictor/retrain_all.sh`
**What:** Master orchestration script. Full pipeline:
1. Download fresh OHLCV from Bybit via ccxt (BTC/ETH/SOL at 1m/5m/15m/1h/4h/1d + mark + funding)
2. Fetch macro data (calls `fetch_macro.py`)
3. Build labeled datasets for BTC, ETH, SOL
4. Train 6 models (BTC/ETH/SOL × long/short)
5. AUC gate: reject any model below `MIN_AUC` (default 0.60)
6. Deploy passing models to `models/`, backup old ones
7. Restart `predictor.service`

**Flags:** `--dry-run`, `--skip-download`, `--skip-macro`, `--min-auc=0.65`
**Cron (weekly):** `0 2 * * 0 cd /opt/predictor && bash retrain_all.sh >> logs/retrain.log 2>&1`

---

### `deploy/predictor.service` ✅
**Path:** `products/Predictor/deploy/predictor.service`
**What:** systemd unit for the predictor server. Runs as `predictor` user, WorkingDirectory `/opt/predictor/src`, uses venv. Memory cap 3GB, CPU quota 200% (2 cores), restarts on failure.

---

### `deploy/macro_refresh.service` + `deploy/macro_refresh.timer` ✅
**Path:** `products/Predictor/deploy/macro_refresh.{service,timer}`
**What:** systemd oneshot service + timer. Runs `fetch_macro.py` every 1h (persistent, survives reboots).

---

### `deploy/nginx.conf` ✅
**Path:** `products/Predictor/deploy/nginx.conf`
**What:** Reverse proxy for port 18910. Routes `/` → dashboard, `/api/` → REST, `/ws` → WebSocket (1h keepalive). Includes commented SSL block for certbot.

---

### `deploy/install.sh` ✅
**Path:** `products/Predictor/deploy/install.sh`
**What:** One-shot VPS setup script. Installs system deps, creates `predictor` user, copies app files, creates venv, installs Python deps, installs + enables systemd units, configures nginx, runs initial macro fetch, starts services. Run as root on fresh Ubuntu 22.04.

---

## Signal Agent — New Files (2026-07-17, session 2)

### `src/signal_agent/__init__.py` ✅
Package marker.

### `src/signal_agent/config.py` ✅
**What:** Config loader. Reads the `signal_agent` block from `config.json`, overridden by environment variables. Key settings:
- `confidence_threshold` (default 0.65) — minimum model probability to trigger enrichment
- `cooldown_seconds` (default 900) — minimum gap between two Claude calls for the same asset
- `claude_model` — `claude-haiku-4-5-20251001` (fast + cheap, suitable for high-frequency checks)
- `anthropic_api_key` — loaded from `ANTHROPIC_API_KEY` env var (never hardcoded)

### `src/signal_agent/enricher.py` ✅
**What:** Two-step enrichment pipeline:
1. `fetch_news()` — DuckDuckGo news search (no API key needed) for asset + macro queries. Up to 6 deduplicated snippets.
2. `call_claude()` — Anthropic Messages API with a structured system prompt. Returns validated JSON (or a safe fallback dict on error). Strips markdown fences from model output.
3. `enrich()` — Public entry point. Returns a structured payload ready to POST to the Predictor.

**Output schema:**
```json
{
  "signal": "BUY|SELL|NEUTRAL|EXIT",
  "confidence_label": "High|Medium|Low",
  "model_context": "...",
  "news_summary": "...",
  "key_risks": "...",
  "analyst_note": "...",
  "asset": "BTC/USDT",
  "long_probability": 0.73,
  "short_probability": 0.12,
  "model_signal": "BUY",
  "news_count": 5,
  "generated_at": "2026-07-17T15:45:00Z"
}
```

### `src/signal_agent/main.py` ✅
**What:** Main loop. Polls `/api/status` every `poll_interval_seconds`. For each asset:
- Checks `_should_enrich()`: signal != NEUTRAL AND max(long_p, short_p) > threshold AND cooldown elapsed
- If triggered: fetches full snapshot via `/api/status?symbol=<asset>`, calls `enrich()`, POSTs result to `/api/enriched-signal/<asset>`
- Tracks `last_enriched[asset]` to enforce per-asset cooldown

**Run:** `python3 -m signal_agent.main` (WorkingDirectory must be `src/`)

### `deploy/signal_agent.service` ✅
**What:** systemd unit for the signal agent. Depends on `predictor.service`. Runs as `predictor` user. Loads env vars from `/opt/predictor/.env` (EnvironmentFile). MemoryMax 512MB, CPUQuota 50%.

### `deploy/install.sh` ✅ MODIFIED
**Changes:**
- Added `anthropic` and `duckduckgo-search` to pip install list
- Creates `/opt/predictor/.env` template with `ANTHROPIC_API_KEY=` placeholder (chmod 600)
- Installs and enables `signal_agent.service`
- Only starts signal_agent if `.env` contains a non-empty `ANTHROPIC_API_KEY`
- Updated install summary to show signal_agent status commands

### `src/predictor_server.py` ✅ MODIFIED
**Changes added:**
- `_enriched_signals: dict[str, dict]` — in-memory store keyed by normalised symbol
- `POST /api/enriched-signal/{asset}` — agent writes enriched signal; triggers WS broadcast
- `GET /api/enriched-signal/{asset}` — dashboard reads latest enriched signal (204 if none yet)
- `GET /api/enriched-signals` — all enriched signals (dashboard overview)
- `HTTPException` + `JSONResponse` added to FastAPI imports

---

## Modified Files

### `src/labels.py` — ✅ NO CHANGE NEEDED
**Path:** `products/Predictor/src/labels.py`
**Finding:** `label_short_tp_before_sl_1h()` already existed here. For a short: TP = entry − tp_atr_mult×ATR − fee_drag, SL = entry + sl_atr_mult×ATR − fee_drag. Returns 1 if price drops to TP before rising to SL within horizon_bars candles.
**Inherited by:** `src/lgbm_poc/labels.py` via `from labels import *` — no duplication needed.

### `src/prepare_full_dataset.py` ✅ MODIFIED (bug fix)
**Change:** Added `label_short_tp_before_sl_1h` import and call in step 5.
**Bug fixed:** Previously only `label_tp_before_sl_1h` was computed. Short models would have trained on the long label — wrong direction.
**Now:** Both labels computed in one pass. `dropna` uses both columns. Class balance printed for each side.

### `retrain_all.sh` ✅ MODIFIED (bug fix)
**Change:** In the training loop, `LABEL` is now set conditionally per `SIDE`:
- `long` → `label_tp_before_sl_1h`
- `short` → `label_short_tp_before_sl_1h`
**Bug fixed:** `LABEL` was hardcoded to the long label for all 6 models. Short models for BTC, ETH, and SOL were being trained on the wrong target.

---

## Files NOT Modified (Proposed Only)

### `products/trading bot/user_data/strategies/LGBMStrategy.py` 📋
Hardening proposals were documented in session but NOT applied. Changes to apply:
- `tp_atr_mult = 1.5` (was 2.0)
- Thread-safe `load_model` with `threading.Lock`
- File mtime check for hot-reload detection
- Feature name logging on load
- BTC-only guard in `populate_indicators`
- `iloc` fix for startup candles
- Nearest-timestamp fallback in `custom_stoploss` / `custom_exit`

### `products/trading bot/user_data/config_lgbm_futures.json` 📋
- `tp_atr_mult: 2.0` → `1.5`
- `"timeframe": "5m"` → `"15m"` (confirm and align)

### `products/trading bot/isolated_lgbm/src/lgbm_poc/baseline.py` ✅
Already implemented: added `tp_atr_mult = 1.5` and `sl_atr_mult = 1.0` to `BaselineSpec`.

### `products/trading bot/isolated_lgbm/src/lgbm_poc/labels.py` ✅
Already implemented: replaced hardcoded `tp_atr_mult=1.5, sl_atr_mult=1.0` defaults with `BASELINE.tp_atr_mult, BASELINE.sl_atr_mult`.

---

## Architecture Decisions Recorded

| Decision | Rationale |
|---|---|
| Unify around Predictor, not Freqtrade bot | Predictor is API-first, already multi-asset, has built-in paper sim. Bot becomes execution adapter later. |
| Advice-only mode = config flag `"mode": "advice"` | No code change needed to switch to live; just toggle + wire execution adapter |
| One model per pair, not shared | Pair-specific calibration more important than shared signal at this scale |
| 1m/5m for micro-structure, NOT for trading signals | Execution stays on 15m; lower TFs add intra-candle precision without overfit risk |
| 1h/4h/1d for trend validation only | Higher TFs gate entry quality, not exit timing |
| Macro: Gold + Oil + DXY + SPX + VIX | All 5 already in July 7 models. Gold (safe haven), Oil (commodity cycle), DXY (USD strength), SPX (risk sentiment), VIX (fear gauge) |
| AUC gate before model deployment | Prevents a bad retrain from overwriting a working model |
| systemd + nginx on VPS | Standard, restartable, log-integrated. No Docker overhead for this scale. |
| Signal agent: event-driven, not scheduled | Only fires Claude when model probability > threshold + signal != NEUTRAL. Minimises inference cost and reduces noise for the client. |
| Signal agent writes to Predictor REST, not directly to dashboard | Single source of truth stays in the Predictor. Dashboard polls `/api/enriched-signal/{asset}`. No coupling between agent and UI. |
| News via DuckDuckGo (no API key) | Zero-cost, zero-registration. Pluggable — swap to NewsAPI or Serper by replacing `_search_ddg()` in enricher.py. |
| 15-minute cooldown between enrichments | Prevents hammering Claude API when model stays above threshold for an extended period. Configurable via `SA_COOLDOWN_SECONDS`. |

---

## Pending / Next Steps

```bash
# ── Local test run (do this first) ────────────────────────────────────────────
cd /path/to/products/Predictor
pip install yfinance ccxt lightgbm pandas numpy scikit-learn pyarrow fastapi uvicorn \
            anthropic duckduckgo-search

# 1. Verify macro fetch works
python3 src/fetch_macro.py --data-dir data/macro --days 730

# 2. Test pipeline logic with existing data (no new download)
bash retrain_all.sh --dry-run

# 3. Full pipeline with existing parquet data (no fresh Bybit download)
bash retrain_all.sh --skip-download

# 4. Full fresh retrain from Bybit
bash retrain_all.sh

# ── VPS deployment ─────────────────────────────────────────────────────────────
scp -r products/Predictor/ user@vps-ip:/tmp/predictor_src
ssh user@vps-ip "sudo bash /tmp/predictor_src/deploy/install.sh"

# After install: fill in the API key, then start the signal agent
ssh user@vps-ip "sudo nano /opt/predictor/.env"
# → set ANTHROPIC_API_KEY=sk-ant-...
ssh user@vps-ip "sudo systemctl start signal_agent"

# Verify:
# curl http://vps-ip/api/status
# curl http://vps-ip/api/enriched-signal/BTC_USDT
# journalctl -u predictor -f
```

- [ ] Validate AUC vs July 7 baseline after retrain (long: 0.7356, short: 0.755 — keep if higher)
- [ ] Verify `requirements.txt` includes `yfinance` and `ccxt`
- [ ] Implement LGBMStrategy.py proposals (after bot work resumes, Antigravity to apply)
- [ ] Wire notification layer (Telegram or similar) for advice delivery to client

---

*Last updated: 2026-07-17 by Claude (Cowork session)*
