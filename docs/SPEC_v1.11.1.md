# SPEC — v1.11.1 — Quarantine v1.11.0 and Establish Reproducible Evaluation

**Stage:** SPEC (loop: Spec → Design → Adversarial Review → Hardening → Approval)
**Author:** Architect (Claude, Cowork)
**Executor:** Google Antigravity / Gemini 3.6 Flash (medium)
**Date:** 2026-07-26
**Status:** AWAITING APPROVAL — no execution until approved

---

## 0. Executive Finding — Why This Spec Is Not A Bug Fix

The v1.11.1 patch described in the handoff (fix `p_short = 1 - p_long` complement
wiring in `predictor_server.py`) **must not be executed**. It is a no-op against a
misdiagnosis. Four independent forensic findings, all verified against files on disk:

### F-1 — The complement is not a wiring bug
`src/predictor_server.py:770-771` already calls each booster independently:
```python
self.latest_prediction_long  = float(self.model_long.predict(X)[-1])
self.latest_prediction_short = float(self.model_short.predict(X)[-1])
```
The string `1 - p_long` does not exist in the file. Signal resolution at lines
775-781 is already byte-for-byte what the handoff prescribes as the fix.

The complement comes from the **labels**. `label_direction_2bar` (`close[t+2] > close[t]`)
and `label_short_direction_2bar` (`close[t+2] < close[t]`) are logical complements.
Same features, same hyperparameters, `random_state=42` → LightGBM builds the mirror image.

Recovered from the serialized boosters (root `internal_value` = -G/H, H = n/4):

| Asset | n rows | n_pos long | n_pos short | sum | tie bars | mirrored? |
|-------|--------|-----------|------------|-----|----------|-----------|
| BTC   | 998    | 500       | 498        | 998 | **0**    | **exactly** |
| ETH   | 998    | 496       | 499        | 995 | 3        | no |
| SOL   | 998    | 492       | 484        | 976 | 22       | no |

BTC has zero tie bars, so gradients are exactly negated and the short booster is the
long booster with every leaf sign-flipped (leaf correlation −1.0000, identical
`split_feature` and `threshold` lines across all 100 trees). ETH/SOL diverge at tree 0
solely because a handful of tie bars break the gradient symmetry.

**There is one directional model, not six.** "Short" is arithmetic, not a second opinion.

### F-2 — The models were trained on 998 rows
Root `internal_count=998` in all six model files. Approximately ten days of 15m candles.
Not the 3-year dataset referenced in `models/metadata.json`, not the 49,053 rows in
`retrain_live_features_report.json`. All six files written within 0.44 s of each other
(2026-07-26 12:18:44) — a single ad-hoc run.

### F-3 — No audit in the handoff is reproducible
AUD-05 through AUD-08 exist **only as prose in `CHANGELOG.md`**. That file is the sole
location in the entire tree containing the literals `0.5801`, `62.3`, `54.2`, `0.0432`,
`0.0668`, `0.0270`.

No file anywhere performs: an ATR TP×SL geometry sweep, a threshold-bucket table, a
fee/net-expectancy calculation, a bootstrap or confidence interval, or a 70:30
chronological directional-accuracy evaluation. No notebooks exist. No `task-*.log` or
result JSON/CSV records any audit run.

Existing machinery is adjacent but produces none of the cited figures:
`src/train_lightgbm.py` (70/15/15 split, AUC/logloss only), `src/evaluate_walkforward.py`
(rolling 200/50/50, AUC/logloss only), `tools/recalibrate_thresholds.py` (fire-rate
percentile), `forge/scoring.py` (live-forge strategies, not Stage 1).

Additionally: the reported AUD-05 asymmetry (short 62.3% vs long 54.2% at the same
T=0.50) is **arithmetically impossible** under F-1. For BTC, mirrored models force
short accuracy ≡ 1 − long accuracy on non-tied bars.

### F-4 — v1.11.0 is not a release and is not committed
`label_direction_2bar` and `label_short_direction_2bar` exist in
`src/lgbm_poc/labels.py:139,153` but **nothing in the codebase imports or calls them**.
`"stage1_version": "v1.11.0"` at `src/predictor_server.py:1272` is a hardcoded display
string; no distinct Stage 1 load path exists.

The working tree at `antigravity-predictor-beta-1.10.32-clean/` is **not a git repository**.
`git tag -l v1.11.0` returns nothing. `CHANGELOG.md` does not exist in
`/media/hermes/Storage/git/antigravity-predictor` at any commit. HEAD there remains
`59936dd beta-1.10.32`. The entire v1.11.0 body of work is an uncommitted file overlay,
in direct violation of the governance rule "all changes originate as repo commits."

The release tarball and SHA256 (`4f3f13…0764`) are real and match — but they package
998-row mirrored models and an unreproducible changelog.

### Conclusion
**v1.11.0 is quarantined, not patched.** EXEC-01's `signal_threshold: 0.56` is derived
from `n=21` signals in an experiment that leaves no trace of ever having run, using
models fit on 998 rows. It must not be treated as validated. The paper-trading unfreeze
gate is unreachable from this state and stays FROZEN.

---

## 1. Root Cause Of The Cost Problem

Every audit in this project was executed as an inline `python3 -c` heredoc inside a chat
session. Nothing was scripted, nothing emitted a durable artifact, nothing was committed.
Therefore **every subsequent question requires a new inference round-trip to re-derive
state that should have been a file on disk.**

That is the actual defect. Not the models, not the wiring. The remedy is that experiments
become committed scripts emitting JSON reports, so verification is `./run_audit.sh` and a
`diff`, not a conversation.

**Governance amendment proposed:** no number may enter `CHANGELOG.md` unless it was
emitted by a committed script into a committed report file, referenced by path.

---

## 2. Scope Of v1.11.1

### In scope
1. Quarantine the v1.11.0 overlay under an explicit, reversible marker.
2. Restore the runtime to the last known-good state (beta-1.10.32).
3. Build a committed, re-runnable Stage 1 evaluation harness.
4. Re-derive AUD-05/06/07/08 honestly, or mark them RETRACTED.

### Explicitly out of scope
- Any change to `predictor_server.py` inference or signal-resolution logic. It is correct.
- Any live or paper execution. Gate stays FROZEN.
- Any threshold tuning. `0.56` is retracted, not re-tuned, until §4 produces a number.
- Any new features or dashboard work.

### Non-negotiable constraints
- No scheduled polling or background tasks (prior quota-exhaustion incident).
- No host config edits. Repo commits only.
- No network calls to exchanges during evaluation; use cached parquet only.

---

## 3. Execution Packets

Packets are deliberately small and mechanical. The executor is a medium-tier model;
each packet must be completable without judgment calls. **Stop at each gate.**

### PACKET A — Quarantine (no analysis, pure bookkeeping)
**A1.** Create git repo state for the overlay. In
`/media/hermes/Storage/git/antigravity-predictor`, create branch `quarantine/v1.11.0`.
Copy in the four overlay-modified files (`CHANGELOG.md`, `config.json`,
`src/lgbm_poc/labels.py`, `src/predictor_server.py`) and `models/*.txt` from
`antigravity-predictor-beta-1.10.32-clean/`. Commit with message
`quarantine: v1.11.0 overlay, unvalidated — see SPEC_v1.11.1.md §0`.
**A2.** Prepend to `CHANGELOG.md` on that branch a `## RETRACTED` block stating that
AUD-05..AUD-08 figures are unreproducible and the models were fit on 998 rows.
**A3.** Do NOT merge to main. Do NOT delete anything. Do NOT ship a v1.11.1 tag yet.

**Acceptance:** `git -C .../git/antigravity-predictor branch --list quarantine/v1.11.0`
returns the branch; `main`/HEAD still at `59936dd`; nothing deleted from disk.

> **GATE A — human approval required before Packet B.**

### PACKET B — Data inventory (read-only, emits a report)
**B1.** Write `tools/audit_data_inventory.py`. For every parquet in
`/media/hermes/Storage/git/antigravity-predictor/.retrain_cache/`, emit to
`reports/data_inventory.json`: file path, row count, first and last timestamp, column
list, count of `close[t+2] == close[t]` tie bars, and the base rate of
`label_direction_2bar`.
**B2.** Run it. Commit script and report together.

**Acceptance:** `reports/data_inventory.json` exists and is committed; it states an
actual usable row count per asset. If usable rows for BTC 15m < 20,000, **halt and
report** — the dataset is insufficient and §4 cannot proceed.

> **GATE B — human approval required before Packet C.**

### PACKET C — Reproducible Stage 1 evaluation harness
**C1.** Write `tools/eval_stage1.py`. It must:
- load cached parquet only (no network),
- build features via the existing `build_features()`,
- apply `label_direction_2bar` (long side ONLY — see §5),
- split chronologically 70:30 with an embargo gap of at least `bars_forward` bars to
  prevent label leakage across the boundary,
- train LightGBM with hyperparameters read from a config block, not hardcoded,
- emit to `reports/stage1_eval_{asset}.json`: n_train, n_test, base rate, tie count,
  test AUC, and a threshold sweep table (T from 0.50 to 0.60 step 0.01) with, per bucket:
  signal count, directional accuracy, gross mean return, and net mean return after a
  fee constant declared in config.
**C2.** Every number must carry its sample size. Any bucket with n < 100 must be
emitted with `"underpowered": true`.
**C3.** Run for BTC, ETH, SOL. Commit scripts and all reports.

**Acceptance:** re-running `tools/eval_stage1.py` twice produces byte-identical reports.
No figure appears in any document without a path to the report that emitted it.

> **GATE C — human approval + adversarial review required before any threshold is adopted.**

---

## 4. Adversarial Review Criteria (apply at Gate C)

The reviewer must attempt to falsify, not confirm. Minimum challenges:

- **Multiple comparisons.** The sweep tests ~11 thresholds × 3 assets. A single surviving
  bucket at n=21 is the expected yield of noise. Require the effect to hold across a
  contiguous run of thresholds, not a single point.
- **Monotonicity.** Accuracy must rise monotonically with threshold. A single spiking
  bucket surrounded by flat neighbours is an artifact.
- **Embargo.** Confirm the train/test boundary has a gap ≥ `bars_forward`. Without it,
  the last training rows carry labels drawn from the first test rows.
- **Fee realism.** Confirm the fee constant is round-trip, not one-way, and that slippage
  is either included or explicitly declared as excluded.
- **Regime concentration.** Segment by ATR tercile and session. If one regime carries the
  result, the result is a regime bet, not an edge.
- **The mirror question.** Confirm whether the short side is still arithmetically implied
  by the long side. If so, report one directional model with a two-sided threshold and
  stop describing it as two models.

---

## 5. Design Decision Requiring Approval

Given F-1, the "two-stage, two-model" architecture as built is one directional model.
Two coherent paths — **the human must pick before Packet C**:

**Option 1 — Own the symmetry (recommended).** Train one `label_direction_2bar` model per
asset. Derive short as `1 - p_long` *explicitly and by design*. Halves training cost,
removes the phantom second model, makes the complement honest rather than a discovered
bug. Requires deleting `label_short_direction_2bar` and simplifying config to one
threshold pair.

**Option 2 — Make the sides genuinely asymmetric.** Give long and short different labels
that are not complements (e.g. different horizons, or a magnitude threshold so that small
moves are 0 on both sides). Only justified if there is a prior reason to expect
directional asymmetry at 15m. Doubles the search space and the multiple-comparisons burden.

Recommendation: **Option 1**. The evidence shows no asymmetry exists; Option 2 invents a
degree of freedom that the data has not asked for.

---

## 6. What Success Looks Like

v1.11.1 ships when, and only when:

- [ ] v1.11.0 overlay is quarantined on a branch, not on main
- [ ] AUD-05..08 are marked RETRACTED or regenerated from committed scripts
- [ ] `reports/data_inventory.json` shows a defensible training set
- [ ] `tools/eval_stage1.py` is committed and produces identical output on re-run
- [ ] Every number in `CHANGELOG.md` cites the report path that emitted it
- [ ] The architecture question in §5 is decided and reflected in code
- [ ] Paper gate remains FROZEN; no threshold is adopted on n < 100

Explicitly NOT success: a green `/api/status` response. The prior handoff's status JSON
mixed keys from two different handlers (`latest_prediction_long`/`latest_signal` from
`/api/status:1165`, `stage1_version`/`execution_mode` from `/api/feature-parity/{symbol}:1247`)
and reported `BUY` at `p_long=0.1523`, which the code at lines 775-781 cannot produce.
That response was very likely never observed. Endpoint output is not evidence.
