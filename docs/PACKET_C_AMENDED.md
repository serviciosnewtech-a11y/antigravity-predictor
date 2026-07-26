# PACKET C — AMENDED SPEC (supersedes plan §Packet C)

**Stage:** HARDENING → ready for execution on approval
**Amends:** `implementation_plan.md` Packet C, after H-13 review
**Date:** 2026-07-26

Packet A and Packet B are complete and verified. This document replaces the Packet C
section of the implementation plan. Amendments C-1 through C-5 are mandatory.

---

## C-1 — Features MUST come from `build_features()`, not from assembled parquet

**This is the H-13 amendment and it is the most important item in this document.**

H-13 (`logs/work_progress/H13_RETRAIN_LIVE_SIGNALS_20260719T0835Z.md`) was caused by the
training side using `lgbm_poc/features.py` while the serving side used `build_features()`
in `predictor_server.py`. The two diverged, 85 of 126 features were silently zero-filled
live, and the predictor froze in `UNAVAILABLE`.

The documented remedy — implemented in `tools/retrain_live_features.py` — is that the
training path imports `build_features()` **directly from `predictor_server.py`**, making
the serving function the single source of truth.

Reading the 84 precomputed columns from `assembled_{ASSET}_USDT.parquet` reintroduces
exactly that divergence. Worse, `feature_gate.py` will not catch it: the gate guards live
inference, not offline evaluation.

**Required:**
- Source data: `.retrain_cache/{ASSET}_USDT.parquet` — the raw 6-column OHLCV file
  (70,102 rows, 2024-07-19 → 2026-07-19), **not** the assembled file.
- Features: computed by importing `build_features()` from `src/predictor_server.py`,
  the same call the live engine makes.
- Follow the pattern already established in `tools/retrain_live_features.py`. Do not
  write a second feature builder.

If `build_features()` cannot be driven offline without network calls, **halt and report**.
Do not substitute the assembled parquet as a workaround.

## C-2 — Feature parity assertion, emitted into every report

```
feature_names := feature_names from the production booster header (65 entries, ordered)
built         := build_features(raw_ohlcv, ...)
assert set(feature_names) ⊆ set(built.columns)      # else HALT, report the missing names
X := built[feature_names]                            # exact list, exact order — LightGBM binds by position
```

Emit into every report: `"feature_names"` (the full ordered list), `"n_features"`, and
`"feature_source": "predictor_server.build_features"`. A report without these three fields
is not a valid Gate C artifact.

## C-3 — Tie policy, declared and counted

Ties (`close[t+2] == close[t]`) are not symmetric across assets: BTC 22, ETH 43, SOL 510
out of 70,102 rows. Under Option 1 they currently fold silently into class 0, biasing the
base rate.

**Required:** declare the policy explicitly and emit `"tie_policy"` (`"exclude"` or
`"count_as_zero"`) plus `"tie_count"` and the resulting `"base_rate"` per asset.
Recommendation: `"exclude"` — a flat bar is not a directional outcome, and SOL's 510 is
large enough to move the base rate measurably.

## C-4 — Null control seed must differ from the training seed

Training uses `random_state=42`. The label-shuffle control must use a different seed.
Emit both as `"train_seed"` and `"shuffle_seed"`.

Acceptance, unchanged and blocking: null test AUC ∈ [0.47, 0.53] and null directional
accuracy ∈ [0.47, 0.53] at every threshold. **If the null run shows signal, the harness is
leaking and every real number it produces is void.** The null run executes first, in the
same script and the same code path as the real run.

## C-5 — Falsification metrics computed by the script, not eyeballed

Emit per asset: `"monotonic"` (bool), `"contiguous_positive_run"` (int — a run of 1 is
noise and must be reported as such), `"atr_tercile_breakdown"`, `"session_breakdown"`,
`"short_is_complement"` (bool, with tie count).

---

## Unchanged from the approved plan

- Option 1 architecture: one `label_direction_2bar` model per asset, short derived as
  `1 - p_long` explicitly by design.
- 70:30 chronological split, `embargo = bars_forward` read from label config.
- Fees: `roundtrip_taker_bps: 11`, `slippage_bps: 0`, echoed into every report.
- Any bucket with n < 100 flagged `"underpowered": true`.
- Determinism: re-run twice, byte-identical output. (Necessary, not sufficient — C-4 is
  the check that actually matters.)
- Commit scripts and reports together.

---

## Gate C acceptance order

Evaluate in this sequence. Any failure halts; do not proceed to the next line.

1. `feature_source` is `build_features`, and 65/65 parity asserted — else VOID
2. Null control within [0.47, 0.53] — else VOID, harness is leaking
3. Determinism: two runs byte-identical
4. Falsification metrics present and populated
5. **Only then** may the real numbers be read

No threshold is adopted at Gate C on n < 100, regardless of how good it looks. Adoption is
a separate decision at a later gate, not a consequence of a passing evaluation.

## Standing rule

No figure enters `CHANGELOG.md`, `config.json`, or any handoff unless it cites the report
path and the dataset SHA256 that produced it. BTC raw 15m source of record:
`482975a719e088682c4280de38bca4601fba1282577959fa627c5013066bcbee`
