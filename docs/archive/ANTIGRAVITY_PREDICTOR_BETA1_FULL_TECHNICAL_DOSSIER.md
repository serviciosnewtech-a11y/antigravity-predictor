# Antigravity Predictor Beta 1 — Full Technical Dossier + Trading Intent

**Generated:** 2026-07-19 UTC  
**Repo:** https://github.com/serviciosnewtech-a11y/antigravity-predictor  
**Verified release:** `main` / `beta-1` at commit `0916e79cdcf98dd68716ac3e031db97f9bee7521`  
**Default safety posture:** dry-run, no exchange credentials, no live trading.

---

## 1. Product intent

Antigravity Predictor Beta 1 is a self-hosted crypto futures signal and activity dashboard.

The intended first operating mode is:

- **Market:** Bybit linear crypto futures data.
- **Assets:** BTC/USDT, ETH/USDT, SOL/USDT.
- **Primary timeframe:** **15 minutes**.
- **Trading style:** short-horizon scalping / intraday signal observation.
- **Decision horizon:** **4 × 15m candles = about 60 minutes max hold**.
- **Purpose:** show model signals, dashboard activity, paper positions/trades, optional AI narrative enrichment.
- **Not intended as:** an autonomous live trading bot.
- **Execution state:** executor exists, but defaults to dry-run and is guarded before live use.

Plain-language intent:

> Watch BTC, ETH, and SOL on 15-minute candles; show BUY/SELL/NEUTRAL/EXIT signals when model probabilities cross calibrated thresholds; simulate entries/exits using ATR-based TP/SL and max-hold rules; optionally enrich strong non-neutral signals with an LLM narrative; keep all order execution in dry-run until intentionally promoted.

---

## 2. Current Beta 1 state

- `main` and tag `beta-1` are pushed at `0916e79cdcf98dd68716ac3e031db97f9bee7521`.
- Fresh `make build` passed after the Beta 1 changes.
- Fresh anonymous clone smoke passed.
- Docker Compose is the primary deploy path.
- The default demo does **not** require Hermes, Ollama, Anthropic, OpenAI, or exchange keys.
- Default `.env.example` keeps:
  - `SA_INFERENCE_BACKEND=disabled`
  - `DRY_RUN=true`
  - empty exchange API key/secret
  - empty `LIVE_CONFIRM`

---

## 3. Runtime architecture

Docker Compose services:

| Service | Exposure | Role |
|---|---:|---|
| `dashboard` | host `${DASHBOARD_PORT:-80}:80` | nginx dashboard + reverse proxy |
| `predictor` | internal Docker network | FastAPI inference server, Bybit WS listener, REST status, dashboard WS |
| `executor` | internal via nginx routes | ccxt execution relay; dry-run by default; mutating routes token-gated |
| `forge` | internal via nginx routes | strategy/paper-testing lab |
| `signal_agent` | no host port | optional AI enrichment poller |
| `retrain` | profile-only | manual model retraining; not always-on |

Beta 1 hardening notes:

- Backend ports `18910`, `18911`, `18912` are not host-published by default.
- Dashboard is the operator/client surface.
- Internal mutating routes require `INTERNAL_API_TOKEN` when configured.
- `DASHBOARD_ORIGINS` defaults to `http://localhost,http://127.0.0.1`.
- No fake AI output: if inference is disabled/unavailable, chat/enrichment should show an honest unavailable state.

---

## 4. Signal/data flow

1. Predictor loads config from `config.json`.
2. Predictor starts asset engines for BTC/USDT, ETH/USDT, SOL/USDT.
3. Each engine loads long and short LightGBM model files from `models/`.
4. Predictor fetches initial Bybit kline candles.
5. Predictor subscribes to Bybit public linear WebSocket topics for the configured 15m timeframe.
6. Each candle update rebuilds the feature table and runs long/short prediction.
7. Signal logic produces `BUY`, `SELL`, `NEUTRAL`, or `EXIT`.
8. Dashboard receives WebSocket ticks and REST status from predictor through nginx.
9. Internal paper simulation opens/closes positions only on confirmed candles.
10. Optional signal-agent polls `/api/status` and posts enriched signal payloads to predictor when a configured threshold/cooldown allows it.
11. Forge can consume predictor WebSocket data and compare strategy variants.

---

## 5. Model and signal baseline

Primary config: `config.json`.

Global baseline:

| Setting | Value |
|---|---:|
| Exchange | `bybit` |
| Timeframe | `15m` |
| Bybit category | linear |
| Prediction assets | BTC/USDT, ETH/USDT, SOL/USDT |
| Min candles before prediction | 50 |
| Initial candles fetched | 150 |
| Rolling candle cap | about 160 |

Current predictor thresholds:

| Asset | Long entry | Long exit | Short entry | Short exit | TP ATR | SL ATR | Max hold | Note |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| BTC/USDT | 0.1898 | 0.1537 | 0.2568 | 0.1981 | 1.5 | 1.0 | 4 candles | long + short active |
| ETH/USDT | 0.1934 | 0.1701 | 0.2149 | 0.1886 | 1.5 | 1.0 | 4 candles | long + short active |
| SOL/USDT | 9.9999 | 9.9999 | 0.2281 | 0.2060 | 1.5 | 1.0 | 4 candles | SOL long disabled; short active |

Signal interpretation:

- If flat and `long_prob >= buy_threshold`, signal is `BUY`.
- If flat and `short_prob >= sell_threshold`, signal is `SELL`.
- If in a long and `long_prob < exit_threshold`, signal is `EXIT`.
- If in a short and `short_prob < exit_short_threshold`, signal is `EXIT`.
- Otherwise signal is `NEUTRAL`.
- Max hold is four confirmed candles, so baseline 15m intent means about one hour maximum paper hold.

---

## 6. Training/label intent

The label code models a simple question:

> Did take-profit hit before stop-loss within the forward horizon?

Defaults from source/metadata:

- `horizon_bars=4`
- `tp_atr_mult=1.5`
- `sl_atr_mult=1.0`
- `round_trip_pct=0.0015` fee/slippage cushion
- timeframe metadata: `15m`

Feature families include:

- 1/3/6 candle log returns
- candle range, body, wick ratios
- ATR proxy and volatility windows
- session/time features
- volume z-score, relative volume, percentile
- EMA fast/slow, trend strength/direction, EMA slope
- liquidity sweep detection
- fair value gap features
- volume breakout/rejection confirmation
- futures mark/funding/basis features
- cross-asset context in training pipeline
- macro context in retrain flow

---

## 7. Safe baseline values for SOUL / trading profile

Recommended SOUL statement:

```text
Antigravity Predictor Beta 1 is a 15-minute crypto futures signal-observation system for BTC/USDT, ETH/USDT, and SOL/USDT. It displays model signals, paper activity, and optional narrative enrichment. It is not authorized for autonomous live trading. Baseline holds are limited to four 15m candles, with ATR-based TP/SL of 1.5/1.0 and dry-run execution only. SOL long is disabled until retraining improves it. Human review owns promotion from signal observation to any execution behavior.
```

Recommended machine baseline:

```yaml
intent:
  mode: dry_run_signal_observation
  market: bybit_linear_crypto
  timeframe: 15m
  horizon_candles: 4
  horizon_minutes: 60
  assets:
    - BTC/USDT
    - ETH/USDT
    - SOL/USDT
  live_trading: false
  execution: dry_run_only

signal_policy:
  btc_long_entry: 0.1898
  btc_long_exit: 0.1537
  btc_short_entry: 0.2568
  btc_short_exit: 0.1981
  eth_long_entry: 0.1934
  eth_long_exit: 0.1701
  eth_short_entry: 0.2149
  eth_short_exit: 0.1886
  sol_long_enabled: false
  sol_short_entry: 0.2281
  sol_short_exit: 0.2060

risk_baseline:
  take_profit_atr: 1.5
  stop_loss_atr: 1.0
  max_candles_held: 4
  round_trip_cost_cushion_pct: 0.0015
  spread_offset_pct_btc_eth: 0.0002
  spread_offset_pct_sol: 0.0003
  simulated_notional_reference: 100_USDT

activity_policy:
  show_dashboard: true
  show_bybit_prices: true
  show_15m_candles: true
  show_model_probabilities: true
  show_buy_sell_neutral_exit: true
  show_paper_positions: true
  show_paper_trades: true
  show_forge_activity: true
  show_agent_report: only_if_inference_backend_configured
```

Hard boundaries:

```yaml
hard_boundaries:
  live_trading: forbidden_by_default
  credentials: not_required_for_demo
  exchange_keys: blank_by_default
  dry_run: required
  live_confirm: must_remain_blank_unless_human_promotes
  executor_mutations: internal_token_required
  llm_enrichment: optional_not_required
  fake_llm_output: forbidden
  sol_long: disabled
```

---

## 8. `.env` defaults for visible dry-run activity

Keep demo-safe values:

```env
DASHBOARD_PORT=80
SA_INFERENCE_BACKEND=disabled
INTERNAL_API_TOKEN=
DASHBOARD_ORIGINS=http://localhost,http://127.0.0.1
SA_CONFIDENCE_THRESHOLD=0.22
SA_COOLDOWN_SECONDS=900
SA_POLL_INTERVAL=30
EXCHANGE=bybit
EXCHANGE_API_KEY=
EXCHANGE_API_SECRET=
DRY_RUN=true
LIVE_CONFIRM=
MIN_LONG_CONF=0.60
MIN_SHORT_CONF=0.60
POSITION_SIZE_PCT=0.02
```

Notes:

- Predictor/dashboard activity does not require executor confidence thresholds to match predictor thresholds.
- Executor thresholds at 0.60 are intentionally conservative and may skip direct predictor-confidence execution requests.
- If the goal is only visible dashboard signals and paper activity, do not lower executor thresholds yet.
- Tune executor thresholds only in a separate paper-execution pass after dashboard signals are visible.

---

## 9. Optional Agent Report / enrichment activity

Default Beta 1 disables inference. To show Agent Report narratives, enable one backend intentionally.

OpenAI-compatible/Hermes-proxy style:

```env
SA_INFERENCE_BACKEND=openai_compatible
HERMES_PROXY_URL=http://host.docker.internal:8645/v1
HERMES_INFERENCE_MODEL=<operator-approved-model>
HERMES_PROXY_API_KEY=local
INTERNAL_API_TOKEN=<same-token-visible-to-predictor-and-signal_agent>
SA_CONFIDENCE_THRESHOLD=0.22
SA_COOLDOWN_SECONDS=900
SA_POLL_INTERVAL=30
```

Activity tuning:

| Goal | Value |
|---|---:|
| calibrated enrichment | `SA_CONFIDENCE_THRESHOLD=0.22` |
| more frequent enrichment | `SA_CONFIDENCE_THRESHOLD=0.20` |
| more selective enrichment | `SA_CONFIDENCE_THRESHOLD=0.25` |
| default cooldown | `900` seconds = 15 min |
| faster testing only | `300` seconds = 5 min |

Do not lower enrichment threshold below 0.20 without evidence; it can create noisy low-quality reports.

---

## 10. Executor baseline

Executor is a dry-run/order-relay surface, not the default system purpose.

Defaults:

| Setting | Default | Meaning |
|---|---:|---|
| `DRY_RUN` | `true` | no real orders |
| `LIVE_CONFIRM` | empty | second gate; live requires `I_ACCEPT_LIVE_TRADING` |
| `MIN_LONG_CONF` | 0.60 | executor ignores weak long requests |
| `MIN_SHORT_CONF` | 0.60 | executor ignores weak short requests |
| `POSITION_SIZE_PCT` | 0.02 | 2% of free USDT if live is ever enabled |
| `EXCHANGE` | bybit | ccxt exchange id |

Live trading requires both:

```env
DRY_RUN=false
LIVE_CONFIRM=I_ACCEPT_LIVE_TRADING
```

and valid exchange credentials. Do not use this for Beta 1 baseline activity.

---

## 11. Forge baseline

Forge is the strategy lab. It runs named parameter sets and records comparisons for human review.

Default strategy families:

- BTC long baseline / tight SL / loose TP / high confidence / scalp
- BTC short baseline / tight SL / high confidence
- ETH long baseline / high confidence / loose TP
- ETH short baseline / high confidence
- SOL short baseline / high confidence / scalp

Default Forge strategy values:

| Value | Baseline |
|---|---:|
| entry_threshold | 0.55 |
| exit_threshold | 0.40 |
| tp_atr_mult | 1.5 |
| sl_atr_mult | 1.0 |
| max_candles_held | 4 |
| scalp tp_atr_mult | 0.8 |
| scalp sl_atr_mult | 0.5 |
| scalp max_candles_held | 2 |

Caveat: Forge thresholds are not the predictor thresholds. Treat Forge as a sandbox, not the canonical live signal calibrator.

---

## 12. Immediate operating plan

To get visible signals/activity first:

1. Deploy Beta 1 from `main` or `beta-1`.
2. Keep `.env` demo-safe.
3. Open dashboard.
4. Confirm dashboard loads.
5. Confirm predictor status via `/api/status`.
6. Confirm paper trades via `/api/trades`.
7. Confirm executor dry-run health via `/executor/health`.
8. Confirm forge health via `/forge/health`.
9. Only after core activity is visible, enable one inference backend for Agent Report narratives.
10. Do not tune live execution until a separate paper-execution baseline is reviewed.

---

## 13. Source files consulted

- `README.md`
- `DOSSIER.md`
- `DOSSIER_TECNICO.md`
- `docker-compose.yml`
- `.env.example`
- `config.json`
- `src/predictor_server.py`
- `src/signal_agent/config.py`
- `src/signal_agent/main.py`
- `src/lgbm_poc/labels.py`
- `src/lgbm_poc/features.py`
- `executor/server.py`
- `forge/strategies.py`
- `models/metadata.json`
- `models/metrics.json`
