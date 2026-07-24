# Antigravity Predictor — Project Dossier
**Last updated:** 2026-07-18  
**GitHub:** https://github.com/serviciosnewtech-a11y/antigravity-predictor (private)  
**Owner:** Luis E. Wilson — serviciosnewtech@gmail.com

---

## What Is This?

A self-hosted, real-time crypto trading signal system for BTC/USDT, ETH/USDT, and SOL/USDT on the 15-minute scalping timeframe. Six LightGBM models (3 assets × long/short) run inside a FastAPI server, streaming live predictions to a browser dashboard over WebSocket.

A dedicated AI enrichment agent (Hermes) monitors model output and fires an LLM call (Ollama or Claude) whenever a high-confidence signal is detected, producing a structured narrative brief — news summary, key risks, analyst note — that appears on the dashboard in real time.

**It is not a trading bot.** It produces signals and reasoning. Order execution is a separate, explicitly gated step (executor service, dry-run by default).

---

## Architecture

```
Client browser
      │ HTTP / WSS (port 80 via nginx)
      ▼
┌─────────────────────────────────────────────────────────────────┐
│  dashboard  (nginx, port 80)                                    │
│  Serves static HTML/CSS/JS. Proxies /api/, /ws, /executor/,    │
│  /forge/ to the appropriate backend containers.                 │
└───────────────────────┬─────────────────────────────────────────┘
                        │ internal Docker network (antigravity)
          ┌─────────────┼─────────────────┐
          ▼             ▼                 ▼
    predictor      executor            forge
    port 18910     port 18911          port 18912
    FastAPI        FastAPI             FastAPI
    LightGBM ×6   Bybit dry-run       Strategy backtester
    WebSocket      order log           SQLite + WebSocket
          │
          │  REST poll every 30s
          ▼
    signal_agent  (Hermes)
    Always-on container, no external port
    Polls /api/status
    → fires LLM when max(long_prob, short_prob) > 0.22
    → POST /api/enriched-signal/{asset}
    → predictor broadcasts via WebSocket
    → dashboard Agent Report panel updates live

    LLM backend (outside Docker, on Ubuntu host):
    • Ollama  (default — no API key required)
    • Anthropic Claude API  (SA_INFERENCE_BACKEND=claude)
    Both reachable from containers via host.docker.internal
```

---

## Services

| Service | Host port | Internal port | Purpose |
|---|---|---|---|
| `dashboard` | 80 | 80 | nginx — static SPA + reverse proxy |
| `predictor` | 18910 | 18910 | LightGBM inference, WebSocket, chat |
| `executor` | 18911 | 18911 | Bybit order relay (dry-run by default) |
| `forge` | 18912 | 18912 | Strategy evaluation / backtester |
| `signal_agent` | — | — | Hermes LLM enrichment agent |
| `retrain` | — | — | Model retrain pipeline (profile-only, run-once) |

---

## File Layout

```
Predictor/
├── docker-compose.yml        ← single source of deployment truth
├── .env.example              ← copy to .env, fill in secrets
├── config.json               ← model paths, thresholds, symbols
├── Makefile                  ← convenience targets (build, up, retrain…)
├── retrain_all.sh            ← full retrain pipeline
├── run_local.sh              ← bare-metal smoke test (non-Docker)
│
├── src/
│   ├── predictor_server.py   ← FastAPI: WebSocket, REST, /api/chat, enriched-signal
│   ├── prepare_full_dataset.py
│   ├── train_lightgbm.py
│   ├── download_ohlcv.py     ← spot / swap / mark_price / funding_rate
│   ├── fetch_macro.py        ← Gold/Oil/DXY/SPX/VIX via yfinance
│   ├── summarize_run.py
│   ├── lgbm_poc/             ← real package: dataset, features, labels, train, evaluate
│   └── signal_agent/
│       ├── main.py           ← Hermes polling loop
│       ├── enricher.py       ← news fetch (DuckDuckGo) + LLM call + JSON parse
│       └── config.py         ← all tunable params, env var overrides
│
├── dashboard/
│   ├── index.html            ← SPA: chart, Agent Report, Hermes chat FAB
│   ├── app.js                ← WebSocket client, signal display, chat wiring
│   ├── style.css             ← dark-theme design system + Hermes chat styles
│   ├── Dockerfile            ← nginx image, copies static files
│   └── nginx.conf            ← proxy rules for all backend services
│
├── predictor/
│   ├── Dockerfile
│   └── requirements.txt
│
├── signal_agent/
│   └── Dockerfile            ← anthropic, duckduckgo-search, requests, loguru
│
├── executor/
│   ├── Dockerfile
│   └── server.py             ← FastAPI, ccxt, Bybit integration, dry-run guard
│
├── forge/
│   ├── Dockerfile
│   ├── server.py
│   ├── collector.py          ← WebSocket feed consumer
│   ├── simulator.py          ← paper trade engine
│   ├── strategies.py
│   └── db.py                 ← SQLite persistence
│
├── retrain/
│   └── Dockerfile            ← full retrain env: lightgbm, ccxt, yfinance, pyarrow
│
├── models/                   ← bind-mounted into predictor + retrain
│   ├── model_btc_long.txt    ← 762 KB, AUC 0.841  (deployed 2026-07-17)
│   ├── model_btc_short.txt   ← 851 KB, AUC 0.864
│   ├── model_eth_long.txt    ← 845 KB, AUC 0.858
│   ├── model_eth_short.txt   ← 666 KB, AUC 0.852
│   ├── model_sol_long.txt    ← 830 KB, AUC 0.848
│   └── model_sol_short.txt   ← 618 KB, AUC 0.845
│
├── deploy/
│   ├── install.sh            ← bare-metal install (venv + systemd, non-Docker path)
│   ├── predictor.service     ← systemd unit (bare-metal fallback)
│   ├── signal_agent.service  ← systemd unit (bare-metal fallback)
│   ├── nginx.conf            ← host-level nginx (bare-metal fallback)
│   ├── macro_refresh.service
│   └── macro_refresh.timer
│
├── data/                     ← bind-mounted into retrain
│   ├── raw/                  ← downloaded OHLCV parquets
│   ├── macro/                ← Gold/Oil/DXY/SPX/VIX CSVs
│   └── datasets/             ← prepared training parquets
│
└── logs/                     ← bind-mounted into predictor + signal_agent
```

---

## Models

Six LightGBM binary classifiers trained on **126 features**. Format: `.txt` (LightGBM native text format — loaded via `lgb.Booster(model_file=...)`).

| Model file | AUC | Notes |
|---|---|---|
| `model_btc_long.txt` | 0.841 | |
| `model_btc_short.txt` | 0.864 | |
| `model_eth_long.txt` | 0.858 | |
| `model_eth_short.txt` | 0.852 | |
| `model_sol_long.txt` | 0.848 | Long disabled in config (win rate < 40%) |
| `model_sol_short.txt` | 0.845 | |

Feature groups: primary pair OHLCV (49) · micro-TF m1/m5 (12) · higher-TF 1h/4h/1d (27) · cross-asset ETH/SOL (8) · macro Gold/Oil/DXY/SPX/VIX (30).

---

## Signal Flow

```
Bybit WebSocket (15m kline stream)
        ↓
AssetEngine.update_candle()
        ↓
build_features()  →  126-feature DataFrame
        ↓
model_long.predict() + model_short.predict()
        ↓
Signal logic: BUY / SELL / NEUTRAL / EXIT
        ↓
manager.broadcast()  →  WebSocket → dashboard updates every tick
        ↓  (async, independent — signal_agent polls separately)
signal_agent polls GET /api/status every 30s
        ↓  when max(long_prob, short_prob) > SA_CONFIDENCE_THRESHOLD (0.22)
            AND signal ≠ NEUTRAL  AND cooldown elapsed
enricher.fetch_news()  →  DuckDuckGo (6 items, last 6h)
enricher.call_ollama() or call_claude()
        ↓  structured JSON:
           { signal, confidence_label, model_context,
             news_summary, key_risks, analyst_note }
        ↓
POST /api/enriched-signal/{asset}
        ↓
predictor stores + broadcasts enriched_signal via WebSocket
        ↓
dashboard Agent Report panel updates live
```

---

## Hermes Chat (Dashboard)

A floating FAB (bottom-right) opens a 340×440px chat panel wired to `POST /api/chat` on the predictor.

- **Available 24/7** — does not require signal_agent to be running.
- **Context per request:** current signal, long/short probabilities, active position, session stats, latest enriched analyst note.
- **LLM path:** `SA_INFERENCE_BACKEND=ollama` → Ollama on host. Falls back to scripted signal-aware replies if Ollama is unreachable.
- **Conversation history** (last 8 turns) sent with each request.
- **Also wired** to the right-sidebar Advisory Chat panel (same `/api/chat` endpoint).

---

## Confidence Threshold — Critical Note

LightGBM models output probabilities in the **0.18–0.28 range** for this dataset. The original placeholder `SA_CONFIDENCE_THRESHOLD=0.65` would never trigger. The calibrated value is **0.22**, set in three places:

1. `src/signal_agent/config.py` — Python default
2. `.env.example` / `.env` — operator-visible  
3. `signal_agent/Dockerfile` — baked into the image as hard fallback

To be more selective: raise to 0.25. To fire on nearly every non-neutral signal: lower to 0.20.

---

## Deployment — Docker (Primary Path)

**Prerequisites on Ubuntu VPS:**
- Docker Engine + Docker Compose plugin
- Ollama installed and running (`ollama serve`) with `ollama pull llama3.1`  
  — OR an Anthropic API key if using `SA_INFERENCE_BACKEND=claude`

```bash
# 1. Clone
git clone https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
cd antigravity-predictor

# 2. Configure
cp .env.example .env
nano .env    # set SA_INFERENCE_BACKEND, ANTHROPIC_API_KEY if using Claude

# 3. Build + start (5 always-on services)
docker compose up -d --build

# 4. Verify
docker compose ps
curl http://localhost/api/status          # through nginx on port 80
curl http://localhost:18910/api/status    # direct to predictor

# 5. Logs
docker compose logs -f signal_agent      # watch enrichment triggers
docker compose logs -f predictor
```

**Client dashboard URL:** `http://<server-ip>/`  (port 80, not 18910)

---

## Deployment — Retrain

Profile-based — not always-on. Run manually when needed:

```bash
docker compose --profile retrain run --rm retrain
# or via Makefile:
make retrain
```

Steps: download OHLCV → fetch macro → prepare dataset → train × 6 → AUC gate (≥ 0.54) → deploy to `models/` → predictor hot-reloads.

Suggested weekly cron on the VPS:
```
0 2 * * 0  cd /opt/predictor && docker compose --profile retrain run --rm retrain >> logs/retrain_cron.log 2>&1
```

---

## Deployment — Bare-Metal Fallback (systemd)

`deploy/install.sh` installs predictor and signal_agent as systemd units. Maintained for environments without Docker; not the primary path.

Key difference from Docker path: `PREDICTOR_URL=http://127.0.0.1:18910` (localhost, not Docker service name).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_PORT` | `80` | Host port for nginx |
| `PREDICTOR_PORT` | `18910` | Host port for predictor (direct access) |
| `EXECUTOR_PORT` | `18911` | Host port for executor |
| `FORGE_PORT` | `18912` | Host port for forge |
| `MODELS_DIR` | `./models` | Bind-mount path for model `.txt` files |
| `DATA_DIR` | `./data` | Bind-mount for training data |
| `LOGS_DIR` | `./logs` | Shared log directory |
| `SA_INFERENCE_BACKEND` | `ollama` | `ollama` or `claude` |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama endpoint (host-accessible from containers) |
| `OLLAMA_MODEL` | `llama3.1` | Model name pulled in Ollama |
| `ANTHROPIC_API_KEY` | *(none)* | Required only if `SA_INFERENCE_BACKEND=claude` |
| `SA_CONFIDENCE_THRESHOLD` | `0.22` | Enrichment trigger level (calibrated to model output range) |
| `SA_COOLDOWN_SECONDS` | `900` | Min seconds between enrichments per asset |
| `SA_POLL_INTERVAL` | `30` | Agent poll frequency (seconds) |
| `EXCHANGE_API_KEY` | *(none)* | Bybit API key (executor only) |
| `EXCHANGE_API_SECRET` | *(none)* | Bybit API secret (executor only) |
| `DRY_RUN` | `true` | Executor never places real orders when true |
| `FORGE_DATA_DIR` | `./forge_data` | Forge SQLite data directory |
| `MIN_AUC` | `0.54` | Retrain quality gate |

---

## API Reference

All endpoints accessible through nginx at port 80 via `/api/` prefix, or directly at port 18910.

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | All-asset signal summary |
| `/api/status?symbol=BTC/USDT` | GET | Single-asset snapshot |
| `/api/candles?symbol=BTC/USDT` | GET | Last 150 OHLCV candles |
| `/api/trades` | GET | Trade simulation history |
| `/api/enriched-signal/{asset}` | GET | Latest Hermes enriched brief |
| `/api/enriched-signal/{asset}` | POST | Used by signal_agent to publish |
| `/api/enriched-signals` | GET | All enriched signals (all assets) |
| `/api/chat` | POST | Hermes interactive chat |
| `/ws` | WebSocket | Live tick + enriched signal broadcast |
| `/executor/status` | GET | Executor health + position |
| `/forge/status` | GET | Forge strategy evaluation state |

---

## Dashboard Features

- Live candlestick chart (lightweight-charts) with signal markers (BUY/SELL/EXIT)
- Asset selector: BTC · ETH · SOL · XAU (demo mode)
- Agent Report panel: signal badge, long/short probability bars, ATR-based TP/SL levels, Hermes enriched brief (model_context, news_summary, key_risks, analyst_note)
- Watchlist with live prices
- Trade estimation log + session stats
- **Hermes chat FAB** (bottom-right): floating chat wired to `/api/chat`, always available
- **Advisory Chat** (right sidebar): same endpoint, alternative entry point
- Drawing tools: trend lines, Fibonacci, shapes, annotations
- 14 right-panel widgets: Watchlist, Alerts, News, Data Window, DOM, Order Panel, Forge, and more
- Light/dark theme toggle

---

## Access Model

| Who | Access | How |
|---|---|---|
| Admin (Luis) | Full — SSH + Docker management | VPS terminal |
| Client | Browser dashboard only | `http://<server-ip>/` |

The client never touches the OS, Docker, or any config file.

---

## What Is Not Yet Done

- [ ] GitHub Actions CI (lint + build test on push)
- [ ] Unit tests for `lgbm_poc/` modules
- [ ] Model versioning (timestamp-stamped files, rollback)
- [ ] TLS / domain on nginx (certbot)
- [ ] Weekly retrain cron (crontab entry — see Retrain section)
- [ ] Freqtrade webhook integration (Predictor signal → bot entry override)

---

*Updated by Claude (Cowork mode) — 2026-07-18*
