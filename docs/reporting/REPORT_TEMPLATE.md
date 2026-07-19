# Paper Report — Week {YYYY-Www} ({start_date} → {end_date} UTC)

**Config commit:** {commit} · **Window day:** {n}/{total} of measurement window
**Uptime:** predictor {pct}% · WS reconnects: {n} · Longest gap: {duration}
**H-13 feature health:** funding_rate {ok/nulls} · microstructure {ok/nulls} · htf_regime {ok/nulls} · cross_asset {ok/nulls}
**Data quality flag:** {CLEAN | DEGRADED — reason}

---

## 1. Leaderboard (all active strategies, pooled per strategy)

| strategy_id | trades | net exp (bps) | win% (resolved) | PF | TP% | SL% | TIMEOUT% | maxDD% | rank |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| main_beta1 | | | | | | | | | |
| {variant} | | | | | | | | | |

*Bars: net breakeven win ≈ 46–50% (resolved trades) · PF target ≥ 1.15 · timeout ≤ 40%.*
*Note: with {N} concurrent variants, top-rank stats are selection-inflated; see §4.*

## 2. Main strategy detail (main_beta1, per asset-side)

| asset-side | trades | cum. sample | net exp (bps) | win% | TP/SL/TO % | maxDD% | sample status |
|---|---:|---:|---:|---:|---|---:|---|
| BTC-L | | /100 | | | | | {OK / INSUFFICIENT} |
| BTC-S | | /100 | | | | | |
| ETH-L | | /100 | | | | | |
| ETH-S | | /100 | | | | | |
| SOL-S | | /100 | | | | | |

## 3. Calibration (main_beta1, pooled, cumulative window)

| entry-prob bucket | n | realized TP-hit % | expected direction |
|---|---:|---:|---|
| 0.19–0.21 | | | baseline |
| 0.21–0.23 | | | ↑ |
| 0.23–0.26 | | | ↑ |
| 0.26+ | | | ↑ |

**Monotonic:** {YES/NO} — {one-line interpretation}

## 4. Segmentation notes (cumulative)

- **ATR tercile:** low {bps} · mid {bps} · high {bps} — edge concentration: {note}
- **Session:** Asia {bps} · EU {bps} · US {bps}
- **Funding ±1h:** in-window {bps} vs out {bps}
- **Variant selection check:** best variant beats main by {x} bps; survives max-of-{N} adjustment: {YES/NO}

## 5. Events & anomalies

- {date} — {ws gap / feature nulls / candle gap / correction records} — {trades tagged}

## 6. Status line (Hermes format)

```
REPORT=WEEK_{Www}
DATA_QUALITY={CLEAN|DEGRADED}
MAIN_NET_EXPECTANCY_BPS={x}
MAIN_CALIBRATION_MONOTONIC={YES|NO}
TIMEOUT_SHARE={x}%
SAMPLE_PROGRESS={x}/100 min asset-side
VARIANTS_ACTIVE={n}
BEST_VARIANT={id}|NONE_SIGNIFICANT
ACTION_REQUIRED={NONE|list}
```

*No mid-window tuning. Observations only; decisions happen at window close.*
