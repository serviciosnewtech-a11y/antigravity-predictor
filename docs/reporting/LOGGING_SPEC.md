# Logging Spec — Parallel Strategy Evaluation (v1)

**Purpose:** one canonical, append-only logging scheme so the main strategy and
all background Forge variants are measured on identical data with identical
cost assumptions, and weekly reports can be compiled mechanically.

---

## 1. Core principles

1. **Append-only JSONL.** No updates, no deletes. Corrections are new records
   with `"corrects": <event_id>`.
2. **Same feed, same costs.** Every variant consumes the same confirmed candles
   and applies the same cost model. A variant may differ ONLY in the parameters
   declared in its registry entry.
3. **`strategy_id` on every record.** The main strategy is just another id
   (`main_beta1`), not a special case.
4. **Frozen variants.** A variant's parameters never change. A tweak = a new
   `strategy_id` (e.g. `btc_scalp_v2`). This preserves the frozen-window rule
   per variant.
5. **One writer per file.** Predictor writes signals/trades for `main_beta1`;
   Forge writes its own variants. No shared file handles across services.

## 2. File layout (volume-mounted, gitignored)

```
logs/
  registry/strategies.jsonl        # variant definitions (append-only)
  signals/YYYY-MM-DD.jsonl         # all signal evaluations, all strategies
  trades/YYYY-MM-DD.jsonl          # opened/closed paper trades, all strategies
  system/YYYY-MM-DD.jsonl          # uptime, WS reconnects, feature-null checks
reports/
  paper/YYYY-Www.md                # weekly compiled report (committed to repo)
```

Daily rotation by UTC date keeps files small and makes gap detection trivial.

## 3. Schemas

### 3.1 Strategy registry (`registry/strategies.jsonl`)
```json
{"event":"strategy_registered","ts":"2026-07-19T00:00:00Z",
 "strategy_id":"main_beta1","engine":"predictor",
 "commit":"0916e79c","status":"active",
 "params":{"assets":["BTC/USDT","ETH/USDT","SOL/USDT"],
   "timeframe":"15m","tp_atr":1.5,"sl_atr":1.0,"max_hold":4,
   "thresholds":{"BTC":{"long":0.1898,"long_exit":0.1537,"short":0.2568,"short_exit":0.1981},
                 "ETH":{"long":0.1934,"long_exit":0.1701,"short":0.2149,"short_exit":0.1886},
                 "SOL":{"long":null,"short":0.2281,"short_exit":0.2060}}},
 "cost_model":{"round_trip_pct":0.0015,"spread_pct":{"BTC":0.0002,"ETH":0.0002,"SOL":0.0003}},
 "notional_ref_usdt":100}
```
Retiring a variant: `{"event":"strategy_retired","strategy_id":"...","ts":"...","reason":"..."}`.

### 3.2 Signal record (`signals/`)
One record per strategy per confirmed candle where a decision was evaluated:
```json
{"event":"signal","ts":"...","strategy_id":"main_beta1",
 "asset":"BTC/USDT","candle_ts":"...","tf":"15m",
 "long_prob":0.2113,"short_prob":0.1470,
 "atr_pct":0.0019,"signal":"BUY","position_state":"flat"}
```
NEUTRAL records included — needed for signal-frequency and calibration analysis.
If volume is a concern, NEUTRAL may be sampled 1-in-N with `"sampled":N` noted.

### 3.3 Trade records (`trades/`)
```json
{"event":"trade_open","ts":"...","strategy_id":"btc_scalp_v1",
 "trade_id":"btc_scalp_v1-2026-07-19-0007",
 "asset":"BTC/USDT","side":"long","entry_px":64231.5,
 "entry_prob":0.2113,"atr_pct_entry":0.0019,
 "tp_px":64424.2,"sl_px":64103.0,"max_hold_candles":2}

{"event":"trade_close","ts":"...","strategy_id":"btc_scalp_v1",
 "trade_id":"btc_scalp_v1-2026-07-19-0007",
 "exit_px":64424.2,"exit_reason":"TP",          
 "hold_candles":1,"gross_pnl_pct":0.0030,
 "net_pnl_pct":0.0015,"net_pnl_usdt_ref":0.15}
```
`exit_reason` ∈ `TP | SL | TIMEOUT | EXIT_SIGNAL`. `trade_id` must be globally
unique and prefix-matched to `strategy_id`.

### 3.4 System records (`system/`)
```json
{"event":"heartbeat","ts":"...","service":"predictor","uptime_s":86400}
{"event":"ws_reconnect","ts":"...","service":"predictor","gap_s":42}
{"event":"feature_check","ts":"...","strategy_id":"main_beta1",
 "nulls":{"funding_rate":0,"microstructure":0,"htf_regime":0,"cross_asset":0}}
```
`feature_check` runs hourly — this is the H-13 monitor in-band.

## 4. Variant discipline

- **Concurrency cap:** ≤ 8 active variants per asset. Beyond that, weekly review
  becomes noise-mining.
- **Multiple-comparisons honesty:** with N variants, the best one's paper result
  is inflated by selection. Rule of thumb: a variant must beat `main_beta1` by a
  margin that survives being the max of N tries (report the pooled rank, not
  just the winner's stats). A winner must then re-prove itself in a fresh
  confirmation window as the *only* candidate before promotion to main.
- **No mid-window edits** (inherits PAPER_BASELINE frozen-window rule per variant).
- **Naming:** `asset_family_vN` (e.g. `eth_tightsl_v1`). Human-readable, greppable.

## 5. Hermes responsibilities (deploy-agent scope)

Hermes MAY: verify log files exist and rotate, run the weekly compiler script,
commit `reports/paper/*.md`, report gaps/nulls in its status format.
Hermes MUST NOT: edit or backfill log records, register/retire/tune variants,
or alter the cost model. Variant changes are repo commits by the admin.
