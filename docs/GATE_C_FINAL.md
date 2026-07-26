# GATE C — FINAL VERDICT

**Date:** 2026-07-26
**Reviewing:** commit `ac1432d`, `reports/stage1_{eval,null,sentinel}_{btc,eth,sol}.json`
**Experiment:** **VALID** — all acceptance criteria met
**Result:** **NEGATIVE** — Stage 1 does not clear taker fees at 15m on any asset
**Consequence:** paper gate stays FROZEN. No threshold adopted. EXEC-01 permanently retracted.

---

## 1. The experiment is now sound

Verified independently against the report files, not the summary:

| Criterion | Result |
|---|---|
| `feature_source` | `predictor_server.build_features` (C-1 satisfied) |
| `n_features` | 65/65 parity asserted |
| `availability_assertion_passed` | `true` on all three assets |
| Null control | BTC 0.5037, ETH 0.5057, SOL 0.4957 — all within [0.47, 0.53] |
| Leak sentinel | sentinel ≈ causal on all three (BTC 0.5335 vs 0.5325) |
| Determinism | byte-identical across runs |
| `tie_policy` | `exclude`, counts 22 / 43 / 510 |
| `embargo_bars` | 2, bound to `bars_forward` |
| Fees | 11.0 bps roundtrip taker, 0 slippage, echoed |

The leak sentinel is the decisive one. Dropping the eight HTF columns now moves AUC by
±0.001. Before the fix it moved it by 0.18. The lookahead is gone.

## 2. The result

| Asset | Test AUC | Base rate | Monotonic | Contiguous positive run | Positive-net buckets |
|---|---|---|---|---|---|
| BTC | 0.5325 | 50.41% | False | 0 | **none** |
| ETH | 0.5291 | 50.62% | False | 0 | **none** |
| SOL | 0.5168 | 50.39% | False | 0 | **none** |

Zero positive-net buckets at any threshold, on any asset. By the falsification criteria
agreed at Gate C — monotonicity, contiguous run > 1 — this fails outright, and it fails the
same way three times independently.

Regime segmentation shows no hiding place. Every ATR tercile and every session is negative,
and tightly clustered:

- BTC terciles: −0.1128 / −0.1120 / −0.1052 %
- BTC sessions: −0.1050 / −0.1156 / −0.1100 %
- Same pattern on ETH and SOL

No single regime is carrying anything, which also means no regime filter can rescue it.

## 3. What is actually true

**The edge is real. It is just far too small.**

AUC 0.5325 on ~21,000 out-of-sample test bars sits roughly 6–7 standard errors above 0.50.
That is a statistically solid directional signal — it is not noise, and it survived a null
control and a leak sentinel.

It is also economically insufficient by roughly 5×. Gross expectancy at mid-range thresholds
runs +0.002% to +0.02% per trade. Round-trip taker cost is 0.11%. The signal is real and the
fee is bigger.

BTC at T=0.60 is the closest approach: gross +0.1098% against an 11 bps fee, net −0.0002%.
Exactly break-even, on 257 signals, at the extreme tail of the sweep. **That is the same
shape as the original n=21 result that started this audit.** It should not be pursued.

## 4. The HTF features contribute nothing

Sentinel AUC (57 features, no HTF) 0.5335 vs causal AUC (65 features, with HTF) 0.5325.
Once made causal, the eight `btc_1h_*` / `btc_4h_*` columns are worth slightly *less* than
zero on BTC.

AUD-04 ranked `btc_1h_return_3` the #1 feature by gain and `btc_4h_return_3` #4. Both
rankings were measuring the leak. The "multicollinearity in the momentum cluster" finding —
that removing the top feature cost no AUC — was two leaking features substituting for each
other, not redundancy among genuine predictors.

## 5. Maker execution does not rescue this

Assuming ~2 bps per side (4 bps round trip — **verify against current Bybit schedule before
relying on this**), only the extreme tail clears:

| | gross | net @ 4bps | n |
|---|---|---|---|
| BTC T=0.58 | +0.066% | +0.026% | 678 |
| BTC T=0.60 | +0.110% | +0.070% | 257 |
| ETH T=0.60 | +0.029% | −0.011% | 362 |
| SOL T=0.60 | +0.060% | +0.020% | 497 |

Two problems. The surviving buckets are tail thresholds with small n — the failure mode this
audit exists to prevent. And AUD-09 already documented the mechanism that eats exactly this
margin: post-only limits at `close[t]` fill preferentially on losers, because winners run
away before filling. A 2–7 bps edge does not survive adverse selection plus a fill rate
below 100%.

## 6. Recommended next step

**Test the same labels at a higher timeframe using this harness.**

Fee is fixed per round trip; the edge scales with the horizon's volatility. Clearing 11 bps
needs roughly 5–10× the current gross expectancy, implying a 2–4 hour horizon rather than
30 minutes. Whether AUC 0.53 persists there is an empirical question — and it is now a cheap
one, because the instrument exists.

This is a config change and a rerun, not a rebuild: point `eval_stage1.py` at
`kline_60m_*` / `kline_240m_*`, set `bars_forward` accordingly, let `embargo` follow it.
Same null control, same sentinel, same falsification metrics.

If a higher timeframe also comes back negative, the correct conclusion is that this feature
set does not support fee-clearing directional prediction on these instruments, and the
project should stop rather than continue tuning.

## 7. What must not happen now

- Do not adopt any threshold from these reports.
- Do not pursue T ≥ 0.60 tail buckets. That is the n=21 trap in a new costume.
- Do not deploy Stage 1 models to the test machine. A clean deployment preflight would
  confirm the machine runs the software; these reports say the model is not worth running.
- Do not retrain to "improve" AUC without a hypothesis. The harness will happily report a
  better number for a subtly leakier feature.

## 8. Standing record

Every figure in this document traces to
`reports/stage1_{eval,null,sentinel}_{btc,eth,sol}.json` at commit `ac1432d`, computed from
`.retrain_cache/BTC_USDT.parquet` SHA256
`482975a719e088682c4280de38bca4601fba1282577959fa627c5013066bcbee`.

This is the first result in this project's history that meets that standard.
