# GATE C — VERDICT: VOID (lookahead leak in HTF features)

**Date:** 2026-07-26
**Reviewing:** commit `42a9ff0`, `tools/eval_stage1.py`, `reports/stage1_eval_{btc,eth,sol}.json`
**Verdict:** **VOID.** Do not adopt any threshold. Do not record any figure from these reports.

Amendments C-1, C-2, C-3, C-5 were implemented correctly. C-4 passed but is blind to this
defect class. The leak is upstream of all of them, in the HTF merge.

---

## 1. The leak

`compute_htf_series()` (`tools/retrain_live_features.py`) labels each 1h/4h feature row with
`df["timestamp"]` — Bybit's kline **open time**. Confirmed from `reports/data_inventory.json`:
`kline_60m_BTC_USDT.parquet` starts `2024-07-19T18:00:00Z` on exact hour boundaries,
`kline_240m` on exact 4-hour boundaries. Open-time convention.

So the 1h row stamped `18:00` describes the bar spanning 18:00–19:00. Its `close` is the
price **at 19:00**.

`merge_htf_onto_grid()` then does:

```python
grid = pd.merge_asof(grid, htf.sort_values("timestamp"), on="timestamp", direction="backward")
```

For a 15m bar at 18:15, the most recent HTF row with `timestamp ≤ 18:15` is the `18:00` row —
whose close is 19:00's price, **45 minutes in the future**.

The label is `close[t+2] > close[t]`, resolving 45 minutes ahead (two 15m bars past the
anchor close).

| 15m bar | Label resolves at | 1h feature's close is at | Leak |
|---|---|---|---|
| :00 | t+45m | t+60m | **entire label horizon, plus 15m** |
| :15 | t+45m | t+45m | **exactly the label horizon** |
| :30 | t+45m | t+30m | partial |
| :45 | t+45m | t+15m | none |

Three bars in four leak. Two in four contain the answer outright. The 4h features leak up to
3h45m the same way.

Affected columns: `btc_1h_return_3`, `btc_1h_trend_strength`, `btc_1h_trend_direction`,
`btc_1h_atr_percentile`, and the four `btc_4h_*` equivalents.

## 2. Why this explains everything

- **AUC 0.7087 on 2-bar direction.** The realistic ceiling for 15m crypto direction is ~0.53.
  0.71 is not an edge; it is a feature that can see the label.
- **AUD-04 ranked `btc_1h_return_3` the #1 feature by gain.** Of course it did — it is the
  most direct carrier of the leak.
- **AUD-04's sensitivity test found that removing `btc_1h_return_3` caused zero AUC
  degradation, and attributed it to multicollinearity.** The better explanation is
  redundant leakage: `btc_4h_return_3` (rank 4) leaks the same future through a longer
  window and substitutes cleanly. Two leaking features covering for each other look exactly
  like a correlated momentum cluster.
- **Monotonic sweeps, contiguous runs of 7–8, and the same clean shape on all three assets.**
  Real 15m edges are noisy and asset-specific. Uniform smoothness across BTC, ETH and SOL is
  the signature of a shared structural artifact — all three use the *same* BTC HTF columns.

## 3. Why the null control did not catch it — my error

I told you C-4 was "the check that actually matters." That was wrong, and it's the reason
this got as far as a Gate C submission.

Shuffling labels destroys **all** feature–label association, leaked or genuine. A harness
with lookahead features will still report null AUC ≈ 0.50, because the shuffle severs the
very relationship the leak exploits. The null control rules out harness misalignment —
predictions compared against wrong indices, test labels bleeding into training. It is
structurally incapable of detecting a feature that contains future information.

The observed null AUCs (0.4998 / 0.5014 / 0.5055) are genuine evidence that the *plumbing*
is correct. They say nothing about feature causality. Both checks are needed; I specified
only one.

## 4. Required fix

Make HTF availability time explicit. An HTF bar is usable only once it has **closed**.

```python
BAR_DURATION = {"1h": pd.Timedelta(hours=1), "4h": pd.Timedelta(hours=4)}

htf["available_at"] = htf["timestamp"] + BAR_DURATION[tf_name]
grid = pd.merge_asof(
    grid.sort_values("timestamp"),
    htf.sort_values("available_at"),
    left_on="timestamp", right_on="available_at",
    direction="backward",          # exact match allowed: at the close instant it IS available
)
```

Apply the same audit to `compute_funding_feature_table()` before rerunning. Funding, mark
and index are point observations rather than bars, so the risk is lower, but the merge
convention must be verified rather than assumed.

**Do not fix by deleting the HTF features.** They are legitimately available live — the live
path (`fetch_btc_htf_context()`) reads the *in-progress* 1h bar, whose close is the current
price. That is causal. Only the offline reconstruction is wrong.

## 5. This is H-13 again, in a new form

H-13 was training-side and serving-side feature builders diverging. This is the same class:
offline HTF reconstruction uses **completed** bars with future closes, while live uses the
**in-progress** bar with the current price. Same feature name, different meaning, training
only. C-1 correctly forced base features through `build_features()` — but HTF is assembled
in `retrain_live_features.py`, outside that function, so C-1 never covered it.

**New standing check, to be added to the harness and to `SPEC_DEPLOY_PREFLIGHT.md` P-02:**
every feature must carry an availability timestamp, and the harness must assert
`availability_time ≤ bar_timestamp` for all 65 columns, emitting the assertion into the
report.

## 6. New Gate C acceptance order

1. HTF availability-time fix applied; funding merge audited
2. Availability assertion emitted for all 65 features
3. Null control still passes (necessary, not sufficient)
4. **Leak sentinel:** rerun with all eight `btc_1h_*` / `btc_4h_*` columns dropped. If AUC
   falls from 0.71 to ~0.52–0.54, the leak is confirmed and the fix is validated. If AUC
   stays high without them, there is a second leak and the hunt continues.
5. Determinism
6. Only then read the numbers

**Expected honest result after the fix: AUC ≈ 0.52–0.54, and net expectancy after 11bps
likely negative at every threshold.** If that is what comes back, it is the correct answer,
not a failure of the work.
