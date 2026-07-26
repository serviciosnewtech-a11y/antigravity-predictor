# ADVERSARIAL REVIEW — Implementation Plan v1.11.1

**Stage:** ADVERSARIAL REVIEW (loop: Spec → Design → **Review** → Hardening → Approval)
**Reviewing:** `implementation_plan.md` / `task.md` (Antigravity / Gemini 3.6 Flash)
**Reviewer:** Architect (Claude, Cowork)
**Date:** 2026-07-26
**Verdict:** **CONDITIONAL — Packet A approved only after R-1 through R-4 are applied. Packet C rejected as written; requires R-7.**

The plan is a faithful restatement of the spec. Every defect below is an omission that
would fail at runtime or silently produce a worthless artifact — not a disagreement.

---

## BLOCKERS — Packet A will fail as written

### R-1 — Stale `.git/index.lock` will abort every git command
`/media/hermes/Storage/git/antigravity-predictor/.git/index.lock` exists, 0 bytes,
created 2026-07-26 12:27. **This is my fault** — my earlier read-only audit ran `git status`
from a sandbox that could not clean up after itself.

Every git operation in Packet A will fail with
`Unable to create '.git/index.lock': File exists`.

**Required first step of A1:**
```bash
R=/media/hermes/Storage/git/antigravity-predictor
# confirm no git process is running, THEN:
rm -f $R/.git/index.lock
git -C $R status   # must succeed before proceeding
```

### R-2 — Do not `git checkout` in the live tree; use a worktree
The plan creates a branch and commits inside the working repository. That repository has
`run.sh`, `models/`, and may be the tree a running predictor process is reading from.
Switching branches under a live process is how you get a half-loaded booster.

**Required:** perform quarantine via `git worktree`, never a branch switch in place:
```bash
git -C $R worktree add ../quarantine-v1.11.0 -b quarantine/v1.11.0
```
Copy overlay files into the worktree, commit there, then `git worktree remove`.
The live tree's HEAD and working files are never touched.

**Also required before A1:** confirm nothing is serving from the repo —
`ss -lptn 'sport = :18910'` or equivalent. If a process holds the port, **halt and report**.

### R-3 — `git add models/*.txt` may silently no-op
`.gitignore` ignores `models/model.txt`, `models/model_long.txt`, `models/model_short.txt`,
`models/model_*_2026070*.txt`, `models/staging/`, `models/backup_*/`. The six files we care
about (`model_btc_long.txt` etc.) do **not** match those patterns, but tracked status is
unconfirmed — `git ls-files models/` returned only `models/archive/**` in the first page.

The 998-row boosters are the **primary evidence** for finding F-2. If `git add` skips them,
the quarantine commit preserves the accusation and destroys the exhibit.

**Required in A1:**
```bash
git -C $R check-ignore -v models/model_btc_long.txt   # expect: no match
git -C $R add -f models/model_{btc,eth,sol}_{long,short}.txt
git -C $R diff --cached --stat   # MUST list all 6 files before commit
```
If all six are not staged, **halt and report**. Do not commit a partial exhibit.

### R-4 — Overlay source directory is never named
A1 says "commit the 4 overlay files" without stating where they come from. They are not in
the git repo. Canonical source, to be stated literally in the task:

```
/media/hermes/Storage/agents/default/workspace/antigravity-predictor-beta-1.10.32-clean/antigravity-predictor-bare-metal-beta-1.10.32/
  ├── CHANGELOG.md
  ├── config.json
  ├── src/lgbm_poc/labels.py
  ├── src/predictor_server.py
  └── models/model_{btc,eth,sol}_{long,short}.txt
```
Copy is one-directional: overlay → worktree. **Nothing is copied back**, and the overlay
directory is not modified or deleted at any point in v1.11.1.

---

## BLOCKERS — Packet B is ambiguous

### R-5 — "all cached parquet files" is not a specification
`.retrain_cache/` holds two plausible BTC sources:

| File | Size |
|---|---|
| `BTC_USDT.parquet` | 2.97 MB |
| `assembled_BTC_USDT.parquet` | 30.78 MB |

plus `kline_60m_*`, `kline_240m_*`, `funding_*`, `index_*`, `mark_*`. The Gate B halt
condition ("BTC 15m rows ≥ 20,000") is meaningless until the canonical 15m OHLCV file is
named. A medium-tier executor will pick one arbitrarily and the gate will pass or fail by
accident.

**Required:** B1 must inventory **every** parquet and report row count, timestamp min/max,
and inferred bar interval (modal timestamp delta) for each. The canonical 15m source is
then **selected by evidence at Gate B, by a human**, not assumed by the script.

### R-6 — The dataset is gitignored and therefore unpreserved
`.gitignore:62` excludes `.retrain_cache/` wholesale. The entire evidentiary basis for all
future evaluation is untracked, unversioned, and unbacked-up. If it is regenerated or
deleted, every number produced in Packet C becomes as unreproducible as AUD-05..08 are now.

**Required in B2:** record in `reports/data_inventory.json`, for each parquet used, its
**SHA256 and byte size**. That hash is what future reports cite. Do not commit the parquets
(they are large and correctly ignored) — commit their hashes.

---

## BLOCKER — Packet C as written cannot detect its own failure

### R-7 — Determinism is not correctness. A null control is mandatory.
The plan's automated verification is "run twice, confirm byte-identical output." A harness
with label leakage, an off-by-one in the embargo, or a lookahead feature will also produce
byte-identical output — twice. This check proves nothing about validity.

**Required addition to C2 — label-shuffle null control.** Before reporting any real result,
`eval_stage1.py` must run the full pipeline a second time with the target labels randomly
permuted (fixed seed) and emit `reports/stage1_null_{asset}.json`.

**Acceptance:** null-run test AUC ∈ [0.47, 0.53] and null directional accuracy at every
threshold ∈ [0.47, 0.53]. **If the null run shows signal, the harness is leaking and every
real number it produces is void.** This gate supersedes all others in Packet C.

This is a hardening of my own spec — SPEC §4 did not require it and should have.

### R-8 — SPEC §4 criteria were demoted to a one-line eyeball check
The plan reduces six falsification tests to "Manual Verification: review for monotonicity,
sample size power, and fee realism." Three §4 criteria are dropped entirely: **regime
concentration**, the **contiguous-threshold-run requirement**, and the **mirror re-check**.

These must be **computed and emitted by the script**, not left to a reader:
- `"monotonic": bool` — accuracy non-decreasing across the threshold sweep
- `"contiguous_positive_run": int` — longest run of consecutive thresholds with positive
  net expectancy. A run of 1 is noise and must be reported as such.
- `"atr_tercile_breakdown"` and `"session_breakdown"` — accuracy and net return per bucket,
  so single-regime dependence is visible without a follow-up query
- `"short_is_complement": bool` — recompute the tie-bar count and state plainly whether the
  short side is arithmetically implied by the long side

### R-9 — Embargo is hardcoded to 2 instead of bound to the label
Plan says "embargo gap ≥ 2 bars." The 2 is `bars_forward` from `label_direction_2bar`. If
anyone changes the horizon, a hardcoded 2 silently reintroduces leakage.

**Required:** `embargo = bars_forward`, read from the same config value the label uses.
Emit both into the report so the relationship is auditable.

### R-10 — The fee constant has no home
C2 must add an explicit `"fees"` block to config — round-trip taker bps, and slippage
either modelled or declared `"slippage_bps": 0` with a note that it is excluded. The report
must echo the fee values it used. AUD-08's `0.11%` currently exists nowhere in code.

---

## MINOR

- **R-11** — `reports/` does not exist. `mkdir -p` in B1, or the first write fails.
- **R-12** — Task C1 (architecture decision) is sequenced inside Packet C, but the plan
  requests the decision now. Harmless; decide at approval, record in config.
- **R-13** — A2's `## RETRACTED` block should cite this review and the spec by path, so the
  retraction is traceable to its evidence rather than asserting itself.

---

## Amended Gate Conditions

**GATE A** — `quarantine/v1.11.0` exists; all six model files present in the commit
(`git show --stat` proves it); `main` at `59936dd`; live tree HEAD unchanged; overlay
directory byte-identical to before; no `index.lock` remaining.

**GATE B** — Inventory covers every parquet with rows, time range, inferred interval, and
SHA256. Canonical 15m source selected by a human on the evidence. Halt if the selected BTC
15m source has < 20,000 usable rows.

**GATE C** — **Null control passes first.** Then: determinism, monotonicity flag,
contiguous run > 1, regime breakdowns present, mirror status stated, fees echoed. No
threshold is adopted on n < 100 regardless of how good it looks.

---

## Standing Instruction

No figure may appear in `CHANGELOG.md`, `config.json`, or any handoff document unless it
cites the report path and dataset SHA256 that produced it. This is the rule whose absence
produced AUD-05 through AUD-08.
