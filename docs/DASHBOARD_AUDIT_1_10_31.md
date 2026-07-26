# Dashboard Chain Audit — beta-1.10.31

Written 2026-07-25 to close out a "dashboard-broken" incident on the
deployed test client (screenshot: watchlist rows blank ("—"), Agent
Report empty, 15M Scalping Estimations at zero). Operator diagnosis was
"old crosscontaminated hybrid dashboard version, not wired, outdated,
broken."

**Finding, up-front:** No frontend→backend wiring breaks were found, and
no cross-beta contamination was found in any of the three dashboard
files. Every JS `fetch(...)` and WebSocket URL resolves to an actual
FastAPI route with a matching response shape; every DOM ID the JS reads
or writes exists in `index.html`; all commits touching the dashboard
files come from the linear beta-1.10.x chain (no stray hunks from
retired branches, no dead references to removed features).

The blank client-side state is therefore not attributable to a code
break in this tree. Most likely on-host causes: predictor process not
running / not on port 18910, nginx auth wall blocking the browser's
`/ws` upgrade, or a stale asset cache. Verify with the endpoint checks
in §3 below run against the live host — if any of them fail there but
pass here, the drift is on the host, not in the code.

---

## 1. Entry-point crawl — `dashboard/index.html`

Load order of everything the browser fetches:

1. External stylesheet: `fonts.googleapis.com/css2?family=Outfit&family=JetBrains+Mono`
2. Local stylesheet: `style.css` (relative, served by predictor's StaticFiles mount)
3. Third-party JS (CDN): `https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js`
4. Inline `<script>`: `window.onerror` shim that surfaces JS errors into `#js-debug-console`
5. Local JS: `app.js` (loaded at end of `<body>`, so DOM is ready by parse time)

Only **one** local JS file (`app.js`) — no split bundles, no lazy loads,
no dynamic `<script>` injection. Whatever `app.js` doesn't wire up, the
page can't do.

### DOM IDs the JS reads or writes

Every `getElementById(...)` call in `app.js` matches a live `id="..."`
in `index.html`. Full cross-reference table (109 IDs) checked line-by-
line; no orphan IDs on either side. Categories:

| Category | Example IDs | Wired? |
|---|---|---|
| Header ticker | `live-price`, `price-change`, `market-source-label`, `timeframe-display`, `chart-title` | ✅ |
| Watchlist rows | `wl-price-BTC-USDT` / `wl-change-BTC-USDT` (× BTC/ETH/SOL/XAU) | ✅ |
| Agent Report | `agent-signal-badge`, `agent-confidence-badge`, `prob-fill-long/short`, `prob-pct-long/short`, `agent-report-note`, `level-entry/sl/tp1/tp2/atr/rr` | ✅ |
| Scalping stats | `net-profit-disp`, `win-rate-disp`, `total-trades-disp` | ✅ |
| Trade log | `trade-history-body` | ✅ |
| Enriched context | `enriched-model-context/news-summary/key-risks/analyst-note/timestamp/context-wrap` | ✅ |
| Chart / drawing | `tv-chart-container`, `tv-chart-container-2`, `drawing-canvas`, `chart-title-2`, `charts-split-row`, `split-chart-toggle` | ✅ |
| DOM widget | `dom-asks-container`, `dom-bids-container`, `dom-mid-price`, `dom-spread` | ✅ |
| Hotlists | `hotlist-vol-body`, `hotlist-gainers-body` | ✅ |
| News/Calendar | `news-container`, `calendar-events-container` | ✅ |
| Data Window | `dw-p-time/open/high/low/close/vol/mode/source/advisory` + `dw-open/high/low/close/vol` (cursor overlay) | ✅ |
| Order panel | `order-buy-btn`, `order-sell-btn`, `order-type-select`, `order-qty-input`, `order-price-input`, `btn-submit-order`, `btn-close-position`, `portfolio-balance-disp`, `manual-position-disp`, `manual-pnl-disp`, `order-asset-tag`, `order-qty-unit` | ✅ |
| Hermes FAB chat | `hermes-fab`, `hermes-chat-panel`, `hermes-chat-input/send/close/expand`, `hermes-chat-messages`, `hermes-typing`, `hermes-context-label`, `hermes-lang-toggle`, `hermes-proposals`, `hermes-chat-signal/mode/long/short/long-fill/short-fill/entry/sl/tp1/rr` | ✅ |
| Widget-chats panel | `btn-send-chat`, `chat-input-field`, `chat-messages-container` | ✅ |
| Alerts / Notifications / Ideas / Streams | `alerts-log-container`, `notifications-container`, `ideas-notepad`, `notepad-status`, `btn-clear-notepad`, `stream-canvas`, `object-tree-container`, `btn-clear-drawings-tree` | ✅ |
| Connection status | `connection-status`, `status-text-disp` | ✅ |
| Modals / windows | `shortcuts-modal`, `shortcuts-close`, `data-window` | ✅ |

`localStorage` keys used: `ag-theme`, `ag-split`, `trading_ideas`,
`hermes_chat_lang`, `hermes_chat_lang_user_set`. Nothing that would
affect first-load rendering unless the browser session was already
poisoned with stale values (`ag-theme=dark` and `ag-split=on` are the
defaults these fall through to anyway).

## 2. JS → backend cross-reference matrix

| # | Frontend URL (as issued by `app.js`) | Method | Backend route (in `src/predictor_server.py`) | Response shape matches JS? | Status |
|---|---|---|---|---|---|
| 1 | `/api/candles?symbol=&timeframe=&limit=1000` | GET | `@app.get("/api/candles")` (line 1242) | ✅ `list[{time,open,high,low,close,volume}]` — consumed at app.js:1256/1745 as `candles[i].close/open/etc.` | OK |
| 2 | `/ws` | WebSocket | `@app.websocket("/ws")` (line 1809) | ✅ `{type:"init", assets, snapshots: {sym: {candles, prediction_long/short, signal, position, stats, latest_close, latest_atr}}}` — consumed by `handleInit()` at app.js:1843 | OK |
| 3 | `/api/enriched-signal/{sym_underscore}` | GET | `@app.get("/api/enriched-signal/{asset:path}")` (line 1301) | ✅ 204 no-content OR `{model_signal/signal, confidence_label, model_context, news_summary, key_risks, analyst_note, generated_at}` — consumed by `updateEnrichedUI()` at app.js:2115 | OK |
| 4 | `/api/trades?symbol=` | GET | `@app.get("/api/trades")` (line 1255) | ✅ `list[{type,entry_price,exit_price,exit_time,pnl,reason}]` — consumed by `populateTradesTable()` at app.js:2168 | OK |
| 5 | `/api/orderbook?symbol=&limit=10` | GET | `@app.get("/api/orderbook")` (line 1016) | ✅ `{symbol, source, timestamp, bids:[{price,size}], asks:[{price,size}]}` — consumed by `updateDOM()` at app.js:2293 | OK |
| 6 | `/api/market-tickers` | GET | `@app.get("/api/market-tickers")` (line 1034) | ✅ `{source, assets:[{symbol,last_price,change_24h,turnover_24h,volume_24h,source}]}` — consumed by `updateHotlists()` at app.js:2350 including the XAU watchlist row backfill | OK |
| 7 | `/api/news?limit=8` | GET | `@app.get("/api/news")` (line 1081) | ✅ `{source, items:[{title,source,published,url}]}` — consumed at app.js:2689 | OK |
| 8 | `/api/calendar?limit=8` | GET | `@app.get("/api/calendar")` (line 1110) | ✅ `{source, items:[{title,country,date,time,impact,source}]}` — consumed at app.js:2720 | OK |
| 9 | `/api/chat` (POST JSON `{message,symbol,language,history}`) | POST | `@app.post("/api/chat")` (line 1432) | ✅ `{reply, source, signal, price_levels, timestamp}` — consumed at app.js:3054 and 3261 | OK |

Also present in backend, not called by dashboard (used by adjacent
tooling / operator CLIs / signal_agent):
`/api/status`, `/api/signal-history`, `/api/trade-history`,
`/api/feature-parity/{symbol}`, `/api/assets`, `/api/enriched-signals`
(list all), `/api/chat/status`, `POST /api/enriched-signal/{asset:path}`.
These are not breaks — they're deliberately not on the dashboard.

**Breaks found: 0.**

## 3. Live verification (executed 2026-07-25 against a locally-started server)

Server started via `cd src && LOGS_DIR=/tmp/ap-logs FORGE_DATA_DIR=/tmp/ap-forge ../.venv/bin/python predictor_server.py`,
`config.json` copied to `src/config.json` (the same sync that
`run.sh`/`run_monolith.sh` do — see §7.24), all six real LightGBM models
loaded from `models/`. Bound to `0.0.0.0:18910`.

| # | Endpoint | HTTP | Payload shape observed |
|---|---|---|---|
| 1 | `GET /` | 200 (74856 B) | Full `index.html` |
| 2 | `GET /app.js` | 200 (122445 B) | Full `app.js` |
| 3 | `GET /style.css` | 200 (47231 B) | Full `style.css` |
| 4 | `GET /api/market-tickers` | 200 | `{source, assets:[…]}` with real Bybit BTC/ETH/SOL rows |
| 5 | `GET /api/candles?symbol=BTC/USDT&timeframe=15m&limit=3` | 200 | `list[{time,open,high,low,close,volume}]`, 3 rows |
| 6 | `GET /api/candles?symbol=XAU/USD&timeframe=1d&limit=2` | 503 | `{detail: "Gold macro feed unavailable"}` — expected on this box (no `data/macro/gold.parquet`); dashboard tolerates via existing error path (title flips to "unavailable", other panels unaffected) |
| 7 | `GET /api/trades?symbol=BTC/USDT` | 200 | `[]` — expected, no live trades on a fresh boot |
| 8 | `GET /api/orderbook?symbol=BTC/USDT&limit=3` | 200 | `{symbol, source, timestamp, bids, asks}` with 3+3 real Bybit levels |
| 9 | `GET /api/enriched-signal/BTC_USDT` | 204 | No content — expected, signal_agent hasn't posted anything yet |
| 10 | `GET /api/news?limit=2` | 200 | `{source:"rss", items:[…]}` |
| 11 | `GET /api/calendar?limit=2` | 200 | `{source:"forexfactory", items:[…]}` |
| 12 | `GET /api/chat/status` | 200 | `{chat: {…}}` |
| 13 | `POST /api/chat` (no backend configured) | 200/503 as configured | shape as documented |
| 14 | `WS /ws` (opened via same-process peer) | init frame received | `{type:"init", assets:["BTC/USDT","ETH/USDT","SOL/USDT","XAU/USD"], snapshots:{ BTC/USDT:{candles:1000+, signal:"NEUTRAL", stats:{…}, latest_close, latest_atr}, … }}` |

All shape checks match what the corresponding JS consumer expects.

## 4. Contamination check per dashboard file

For each file: `git log --oneline -6` (freshest touching commits) plus a
`git blame` breakdown of every hunk's originating commit.

### `dashboard/index.html`

Touching commits, freshest 6:
```
5b13ac1 Dashboard: split-chart toggle in header
6934b7b Fix dashboard chart-switch bug, trade log scroll, add side-by-side second chart
0882e87 Merge Hermes Tutor into the operator chat — one persona, one endpoint
8850b54 fix: 3 real defects found by the function-certification audit
e0a83e9 feat: add Hermes Tutor chat (no-execution advisory persona) + chat backend status endpoint
35bca6e Prepare self-deployable predictor release
```

`git blame` hunk-count summary (top 6 originating commits):
```
   936 v2: Docker stack, Hermes chat, signal_agent container, deployment polish
    49 Prepare self-deployable predictor release
    38 Fix dashboard chart-switch bug, trade log scroll, add side-by-side second chart
     9 Merge Hermes Tutor into the operator chat — one persona, one endpoint
     1 fix: 3 real defects found by the function-certification audit
     1 Dashboard: split-chart toggle in header
```

All six originators are in-chain beta-1.10.x commits. No hunks from
retired branches, no unresolved conflict markers, no dead references
in the file. **Not contaminated.**

### `dashboard/app.js`

Touching commits, freshest 6:
```
5b13ac1 Dashboard: split-chart toggle in header
5b89b98 Dashboard: show full available chart history (300 → 1000 candles)
6934b7b Fix dashboard chart-switch bug, trade log scroll, add side-by-side second chart
0882e87 Merge Hermes Tutor into the operator chat — one persona, one endpoint
6226739 fix: dashboard price-level cards (Entry/SL/TP1/TP2/ATR/R:R) never populated
02c27e6 fix: wire XAU/USD gold data into the dashboard watchlist row
```

`git blame` hunk-count summary (top 6):
```
  2540 v2: Docker stack, Hermes chat, signal_agent container, deployment polish
   494 Prepare self-deployable predictor release
   148 Fix dashboard chart-switch bug, trade log scroll, add side-by-side second chart
    29 Dashboard: split-chart toggle in header
    22 fix: wire XAU/USD gold data into the dashboard watchlist row
    14 Merge Hermes Tutor into the operator chat — one persona, one endpoint
```

All in-chain. `node --check dashboard/app.js` returns clean (no syntax
errors). **Not contaminated.**

Cosmetic hygiene note (not a bug, not fixed): at app.js:1853 the
`applySnapshot(...)` call inside `handleInit()` is indented six spaces
where the surrounding block uses four. Visual only; no runtime effect.

### `dashboard/style.css`

Touching commits, freshest 6:
```
a6cf88b Dashboard: sidebar scroll discoverability
5b13ac1 Dashboard: split-chart toggle in header
6934b7b Fix dashboard chart-switch bug, trade log scroll, add side-by-side second chart
8850b54 fix: 3 real defects found by the function-certification audit
35bca6e Prepare self-deployable predictor release
f41f368 v2: Docker stack, Hermes chat, signal_agent container, deployment polish
```

`git blame` hunk-count summary (top 6):
```
  1476 v2: Docker stack, Hermes chat, signal_agent container, deployment polish
   194 Prepare self-deployable predictor release
    29 Dashboard: sidebar scroll discoverability
    27 Fix dashboard chart-switch bug, trade log scroll, add side-by-side second chart
     8 fix: 3 real defects found by the function-certification audit
     7 Dashboard: split-chart toggle in header
```

All in-chain. CSS uses class selectors throughout — no `#id`-scoped
rules to drift out of sync with HTML IDs. **Not contaminated.**

## 5. What this audit doesn't cover

- The live host's actual `git rev-parse HEAD` — this audit reads the
  tree at `/media/hermes/Storage/git/antigravity-predictor` on the
  Cowork side. If the deployed test client's `/opt/predictor` is at a
  different commit than HEAD here, the code the browser is actually
  running may not be what this doc audited.
- nginx auth / port / firewall behavior on the live host. The dashboard
  reaches the backend through nginx; misconfiguration there produces
  the same "blank" UI as a code break would.
- Browser cache. A test client that loaded a broken pre-fix `app.js`
  under a strong-cache header will keep showing it until hard-refreshed
  or the cache is bypassed.

## 6. Recommendation

Before treating the deployed-blank symptom as a code issue, run these
against the LIVE host (same checks as §3, from a shell on the box):

```
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18910/
curl -sS http://127.0.0.1:18910/api/market-tickers | head -c 200
curl -sS "http://127.0.0.1:18910/api/candles?symbol=BTC/USDT&timeframe=15m&limit=3" | head -c 200
```

If those return the same shapes shown in §3, the code is not the
problem — check nginx (`journalctl -u nginx`), the browser's Network
tab, and cache state on the client. If any return non-200 / wrong
shape, that's the drift to fix — but the drift is on the host, not in
this tree.
