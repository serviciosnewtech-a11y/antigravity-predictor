# Data Inventory — Antigravity Predictor

Written 2026-07-24 against tag `beta-1.10.19` to ground the next 3-4 tags of
backup / off-host / restore work. Companion to `HANDOFF.md`; where they
disagree, verify against code — HANDOFF is a running narrative, this is a
point-in-time audit.

Goal: for every piece of durable data this system holds or generates, know
(a) where it lives, (b) how it's created, (c) how much we'd cry if we lost
it, (d) whether it's protected today, (e) how to bring it back.

**Ambition (Luis, not yet implemented):** eventual off-host backup target is
a personal cloud accessed from the VPS. Nothing is built for that yet. Every
"Off-host: TBD (personal cloud)" line below flags where that hook will go
once designed — the current implementation is local-disk redundancy only.

Paths are given as **in-repo (relative)** followed by **on the live host
(`/opt/predictor/...`)**, since the two products (bare-metal / Docker)
resolve the same relative path to two different absolute prefixes.

---

## 0. One-glance summary table

Tiers: **irrepl** = irreplaceable (cannot be recomputed), **regen-slow** =
regeneratable but expensive/slow (hours of retraining or accumulated live
capture), **regen-fast** = trivially regeneratable (seconds/minutes),
**ephemeral** = fine to lose.

Coverage: **off** = off-host backup exists, **local** = local-disk snapshot
exists (via one of the two `*_backup.timer` units), **none** = single copy,
no backup at all.

| # | Source | Live path | Tier | Coverage | Silent-loss? |
|---|---|---|---|---|---|
| 1 | `signal_history.db` | `/opt/predictor/logs/signal_history.db` | **irrepl** | local (6h) | Yes — only visible on next restart's `load_history()` |
| 2 | `forge.db` (trades + candles + registry) | `/opt/predictor/forge_data/forge.db` | **irrepl** | local (6h) | Yes — only visible when scorecard/API next reads |
| 3 | `forge.db` scorecard tables | inside #2 | regen-slow | local (via #2) | Yes — until next scorecard run |
| 4 | Persona / Hermes shared memory | `/opt/predictor/logs/crypto_operator_memory.jsonl` | **irrepl** | **none** | Yes — silent |
| 5 | LightGBM production models (6 × `.txt`) | `/opt/predictor/models/model_*.txt` | regen-slow | **none** (only ad-hoc pre-retrain snapshots in repo) | Loud at next restart (predictor crashes / degrades) |
| 6 | `config.json` (thresholds, ATR mults) | `/opt/predictor/config.json` + `/opt/predictor/src/config.json` | regen-slow | **none** (git only) | Silent (defaults to whatever's committed at time of reinstall) |
| 7 | `models/metadata.json`, `metrics.json`, `retrain_live_features_report.json`, `recalibrate_thresholds_report.json`, `training_report_*.json` | `/opt/predictor/models/*.json` | regen-slow | **none** | Silent (informational, no runtime dependency) |
| 8 | Macro parquet feeds (gold/oil/dxy/spx/vix) | `/opt/predictor/data/macro/*.parquet` | regen-fast | **none** (hourly refresh via `macro_refresh.timer`) | Loud (Gold panel + market-tickers degrade until next refresh) |
| 9 | `.env` (secrets, tuning knobs, `INTERNAL_API_TOKEN`, `BASIC_AUTH_*` if set) | `/opt/predictor/.env` | **irrepl** (`INTERNAL_API_TOKEN` if never re-generated elsewhere) | **none** (git-ignored) | Loud (services degrade / auth fails) |
| 10 | `/etc/nginx/.htpasswd` (basic auth) | `/etc/nginx/.htpasswd` | **irrepl** (password is printed once during install, never re-derivable) | **none** | Loud (dashboard 401 until reset) |
| 11 | Scorecard human-readable dump | `/opt/predictor-forge-scorecard/scorecard.txt` | regen-fast (rebuilt from #2 on next `forge_scorecard.timer` tick, ≤1h) | **none** (regen source is backed up in #2) | n/a (regenerated) |
| 12 | Service logs | `/opt/predictor/logs/*.log` | ephemeral (rich for post-mortem) | **none** (rotated only by growth) | Loud only during an active incident |
| 13 | `.retrain_cache/*.parquet` | `/opt/predictor/.retrain_cache/` | regen-slow (multi-hour re-download from Bybit) | **none** (git-ignored, retrain-only) | Loud during a retrain (adds ~hours), silent otherwise |
| 14 | Raw OHLCV data (retrain input) | `/opt/predictor/data/raw/*.parquet` | regen-slow (Bybit re-fetch) | **none** | Loud during a retrain |
| 15 | Training datasets | `/opt/predictor/data/datasets/*.parquet` | regen-slow (rebuilt from #14 by `retrain_all.sh` step 3) | **none** | Loud during a retrain |
| 16 | Ad-hoc `models/backup_pre_*/` | in-repo only | ephemeral | **none** | Silent — informational history only |
| 17 | Signal-agent runtime state (`last_enriched` cooldown map) | RAM only | ephemeral | n/a | Silent (loses cooldown timing across restart; not durable by design) |
| 18 | Forge in-memory candle deques | RAM only | ephemeral | n/a | Silent (bounded to 19 candles per symbol; rebuilt within seconds of next tick) |
| 19 | Backup snapshots directory itself | `/opt/predictor-backups/` | derived from #1/#2 | **none** (**this is the single point where off-host sync needs to land**) | Silent — the whole disaster-recovery story dies here if the host is gone |

Off-host coverage today is **zero** across every row. Every "local" tier
guards against `rm -rf /opt/predictor` or a bad `install.sh` re-run only,
not against loss of the whole host/disk.

---

## 1. `signal_history.db` — every signal transition and simulated trade

- **Path (in-repo):** `logs/signal_history.db`
- **Path (live):** `/opt/predictor/logs/signal_history.db`
- **What it holds:** Two tables — `trades` (one row per completed simulated
  trade: symbol, direction, entry/exit time+price, PnL, exit reason) and
  `signal_events` (one row per signal transition: BUY/SELL/NEUTRAL/EXIT/
  UNAVAILABLE, long/short probs, price, ATR, degraded flag). This is the
  only durable record of "what did the predictor actually say/do?" over its
  entire lifetime.
- **How it's created / written to:**
  - Schema init: `signal_log.init_db()`, called from
    `src/predictor_server.py`'s `startup_event` (line 973).
  - Written by `AssetEngine._tick()` in `src/predictor_server.py`:
    `signal_log.record_signal_event()` at line 794 (every tick),
    `signal_log.record_trade()` at line 853 (every closed position).
  - Module: `src/signal_log.py` (path resolved via `LOGS_DIR` env override
    or `<src>/../logs`).
- **Value tier:** **Irreplaceable.** Every row not saved here is lost —
  Bybit doesn't emit "what the model thought at 15:00" and never will. This
  is also the substrate for eventual threshold retuning against real live
  performance.
- **Failure profile if lost/corrupted:** **Silent** short-term.
  `predictor.service` continues running with an empty DB; the next process
  restart calls `AssetEngine.load_history()`/`signal_log.get_stats()` and
  reseeds `total_pnl`/`win_trades`/`loss_trades` to zero for every symbol —
  so the dashboard's per-symbol trade history and PnL counters snap to zero
  after that restart, which is when the operator notices. Not caught by any
  active healthcheck.
- **Backup coverage (local):** `tools/backup_signal_log.py` +
  `deploy/bare-metal/predictor_backup.{service,timer}`. Uses SQLite's own
  online backup API (safe against a live in-use DB, unlike `cp`) into
  `/opt/predictor-backups/signal_history.YYYYMMDD-HHMMSS-uuuuuu.db`. Fires
  5 min after boot then every 6 h.
- **Backup coverage (off-host):** **None.** Off-host target: TBD (personal
  cloud). Hook point is `BACKUP_DIR` (default `/opt/predictor-backups`).
- **Retention:** `BACKUP_RETENTION_COUNT` env var, default **30 snapshots**
  (~7.5 days at 6h cadence). Pruned by `_prune_old_backups()` scoped to
  `signal_history.*.db` glob.
- **Restore steps:**
  ```bash
  systemctl stop predictor.service signal_agent.service
  # Pick the newest snapshot, or a specific timestamp:
  LATEST=$(ls -1t /opt/predictor-backups/signal_history.*.db | head -1)
  cp "$LATEST" /opt/predictor/logs/signal_history.db
  chown predictor:predictor /opt/predictor/logs/signal_history.db
  chmod 600 /opt/predictor/logs/signal_history.db
  systemctl start predictor.service signal_agent.service
  # Verify: journalctl -u predictor --since "1 minute ago" | grep signal_log
  ```

---

## 2. `forge.db` — paper-trading history, strategy registry, scorecard

- **Path (in-repo):** `forge_data/forge.db`
- **Path (live):** `/opt/predictor/forge_data/forge.db`
- **What it holds:** Five tables:
  - `candles` — rolling per-symbol OHLCV + model outputs (long_prob,
    short_prob, atr, trend), pruned to last **5000 rows per symbol**
    (`forge/db.py:insert_candle`). Effectively a live-tick cache, not a
    long-term archive.
  - `trades` — one row per closed simulated position across all 16
    strategies (strategy_id, symbol, direction, entry/exit ts+price,
    tp_price, sl_price, exit_reason, pnl_pct, candles_held, entry_conf,
    nullable `model_version` + `strategy_version`).
  - `strategy_registry` — one row per canonical strategy (deterministic
    UUID5 id — see §7.10 in HANDOFF).
  - `strategy_scorecard` — current per-strategy verdict, one row per
    strategy, overwritten each scoring run.
  - `evaluation_history` — append-only audit trail of every scorecard run
    (substrate for future trend-based verdicts).
- **How it's created / written to:**
  - Init: `forge/db.py:init_db()` — enables `PRAGMA journal_mode=WAL`
    (persistent, one-time) and sets `synchronous=NORMAL` on every
    connection (per-conn, in `_conn()`).
  - Writers:
    - `forge/db.py:insert_candle()` from `forge/collector.py` per tick.
    - `forge/db.py:open_trade()` / `close_trade()` from
      `forge/simulator.py:StrategySimulator.on_tick`.
    - `forge/db.py:upsert_strategy()` on strategy registration.
    - `forge/scoring.py:score_all_strategies()` writes
      `strategy_scorecard` + `evaluation_history` via
      `tools/forge_scorecard.py` on `forge_scorecard.timer` (hourly).
- **Value tier:** **Irreplaceable** for the `trades` and
  `evaluation_history` history. `candles` is a rolling buffer (regen-fast).
  Losing `evaluation_history` also loses the runway toward v2 trend-based
  verdicts (`recovering` / `degrading` — deferred, see HANDOFF §7.10).
- **Failure profile:** **Silent.** `forge.service` (or its Docker
  equivalent) continues running with an empty DB; the next scorecard tick
  populates fresh rows against zero history, so every strategy flags
  `not_enough_data` for at least the next 50-trade window. Registry
  cleanup on next start is a safe no-op on empty DB.
- **Backup coverage (local):** `tools/backup_forge_db.py` +
  `deploy/bare-metal/forge_backup.{service,timer}`. Same design as
  `signal_history` backup: online backup API into the **same**
  `/opt/predictor-backups/` directory, filename prefix `forge.*.db`
  distinguishes them. Fires 7 min after boot then every 6 h. Retention
  glob is scoped so `forge`'s prune never touches `signal_history`
  snapshots.
- **Backup coverage (off-host):** **None.** Off-host target: TBD.
- **Retention:** `FORGE_BACKUP_RETENTION_COUNT`, default **30 snapshots**
  (independent of #1's retention).
- **Restore steps:**
  ```bash
  systemctl stop forge.service       # (docker: docker compose stop forge)
  LATEST=$(ls -1t /opt/predictor-backups/forge.*.db | head -1)
  cp "$LATEST" /opt/predictor/forge_data/forge.db
  chown predictor:predictor /opt/predictor/forge_data/forge.db
  chmod 600 /opt/predictor/forge_data/forge.db
  systemctl start forge.service
  # Verify: /opt/predictor/.venv/bin/python /opt/predictor/tools/forge_scorecard.py
  ```

---

## 3. Persona / Hermes shared memory (`crypto_operator_memory.jsonl`)

- **Path (in-repo):** `logs/crypto_operator_memory.jsonl`
- **Path (live):** `/opt/predictor/logs/crypto_operator_memory.jsonl`
- **What it holds:** Append-only JSONL, one line per exchange. Both surfaces
  write here: interactive dashboard chat (`/api/chat` via
  `hermes_persona.memory_append`) and automated signal-triggered
  enrichment digests (`hermes_persona.record_enrichment_digest`). Recent
  12 exchanges are recalled into the system prompt on every new call
  (bounded rotation cap at 2000 lines to keep disk growth finite).
- **How it's created / written to:**
  - `src/hermes_persona.py:memory_append()` — called from
    `predictor_server._record_memory()` (line 1640) and from
    `signal_agent/enricher.py` via `record_enrichment_digest()`.
  - Best-effort writes only: a failure logs a warning but does not break
    the caller's response.
- **Value tier:** **Irreplaceable.** Free-text history of every operator ↔
  Hermes exchange and every automated enrichment digest — impossible to
  recover from anywhere else.
- **Failure profile:** **Silent.** Missing file → `memory_recall()` returns
  empty string → Hermes answers with no historical context, no warning
  surfaced. The operator only notices "Hermes forgot everything" when they
  refer back to a prior exchange.
- **Backup coverage (local):** **NONE.** This is a real gap — every other
  irreplaceable durable data source (`signal_history.db`, `forge.db`) has
  a 6h backup timer; this doesn't.
- **Backup coverage (off-host):** **None.** Off-host target: TBD.
- **Retention:** in-app rotation only — capped at 2000 lines by
  `memory_append()` when it notices the cap is exceeded.
- **Restore steps:** **n/a — there is nothing to restore from.** If lost,
  Hermes starts with empty memory. First real backup work should add a
  fourth `tools/backup_persona_memory.py` in the same pattern as
  `backup_signal_log.py` / `backup_forge_db.py`, dumping to the same
  `/opt/predictor-backups/` directory (filename prefix
  `crypto_operator_memory.*.jsonl`).

---

## 4. LightGBM production models (`models/model_*.txt`)

- **Path (in-repo):** `models/model_{btc,eth,sol}_{long,short}.txt` (6 files)
- **Path (live):** `/opt/predictor/models/model_*.txt`
- **What it holds:** The six trained LightGBM boosters used by
  `predictor_server.py:AssetEngine.load_models()`. Text-format serialised
  boosters (`model.booster_.save_model()`), ~270-500 KB each.
- **How it's created / written to:**
  - Full retrain: `retrain_all.sh` (steps 4-5, saves to
    `models/staging/{key}_{side}/model/model.txt`, then step 6 copies
    passing models — those clearing `MIN_AUC=0.54` — into
    `models/model_{key}_{side}.txt`, and snapshots the previous set into
    `models/backup_${TS}/`).
  - Live-features retrain: `tools/retrain_live_features.py:659` calls
    `model.booster_.save_model(str(out_path))` directly into the models
    dir (bypasses staging).
- **Value tier:** **Regeneratable-but-slow.** A full retrain from
  `retrain_all.sh` is multi-hour and depends on fresh Bybit history +
  yfinance macro pulls. `tools/retrain_live_features.py` is faster
  (uses `.retrain_cache/`), still not fast.
- **Failure profile:** **Loud, at next restart.** Missing model files
  raise on `AssetEngine.load_models()` — `predictor.service` will fail to
  start or crash-loop. Present-but-corrupted files raise on LightGBM's
  `Booster(model_file=...)` call.
- **Backup coverage (local):** **NONE** for a persistent snapshot outside
  the app dir. The only existing on-host snapshots are:
  - `retrain_all.sh` writes `models/backup_${TS}/` inside the models dir
    itself on every retrain, but this is INSIDE `/opt/predictor` and dies
    with an `rm -rf`.
  - Three `models/backup_pre_*/` directories in the git repo (2026-07-19)
    are ad-hoc pre-retrain safety copies from that session — not part of
    the shipped retrain flow, informational only.
- **Backup coverage (off-host):** **None.** Off-host target: TBD.
- **Retention:** All old `backup_${TS}/` directories accumulate
  unbounded — no pruning. Bounded only by disk and manual cleanup.
- **Restore steps:**
  ```bash
  systemctl stop predictor.service
  # Pick the most recent /opt/predictor/models/backup_YYYYMMDD_HHMMSS/
  LATEST=$(ls -1td /opt/predictor/models/backup_* | head -1)
  cp "$LATEST"/model_*.txt /opt/predictor/models/
  chown predictor:predictor /opt/predictor/models/model_*.txt
  systemctl start predictor.service
  # Or: full re-retrain if backups also gone —
  #   cd /opt/predictor && bash retrain_all.sh   (multi-hour)
  ```

---

## 5. `config.json` — asset thresholds, ATR multipliers, model paths

- **Path (in-repo):** `config.json` (canonical) — copied to `src/config.json`
  by `install.sh` / `run_local.sh` / `run_monolith.sh` / Dockerfile
- **Path (live):** `/opt/predictor/config.json` and `/opt/predictor/src/config.json`
- **What it holds:** Per-asset buy/sell/exit thresholds, TP/SL ATR
  multipliers, `max_candles_held`, server host/port. Thresholds are
  calibrated (see `models/recalibrate_thresholds_report.json`) — a plain
  reinstall reverts them to whatever's committed in git at that moment.
- **How it's created / written to:**
  - `install.sh` copies the packaged `config.json` and forces
    `server.host = 127.0.0.1` via `sed` (bare-metal loopback bind, see
    HANDOFF §7.6).
  - `tools/recalibrate_thresholds.py:145` — `config_path.write_text()` —
    rewrites `config.json` in place after computing new thresholds from
    the current models.
- **Value tier:** **Regeneratable-but-slow.** Reproducible by re-running
  `tools/recalibrate_thresholds.py --fire-rate 0.65` against the current
  models + cached OHLCV.
- **Failure profile:** **Silent.** If overwritten with a stale/wrong
  version, predictor happily loads it and starts firing at the wrong
  thresholds — no signal in the logs would surface this until the operator
  notices signals aren't matching what they expect.
- **Backup coverage (local):** **None** beyond git. The
  `models/backup_pre_htf_history_expand_20260719_170000/config.json.bak`
  is a one-off, not part of any repeating job.
- **Backup coverage (off-host):** **None.** Git remote is the closest
  thing but only holds committed changes.
- **Retention:** git tag history only.
- **Restore steps:** copy from git (`git show beta-1.10.19:config.json >
  /opt/predictor/config.json`) or re-run
  `tools/recalibrate_thresholds.py`.

---

## 6. Model metadata / metrics / retrain reports

- **Path (in-repo/live):** `models/metadata.json`, `models/metrics.json`,
  `models/retrain_live_features_report.json`,
  `models/recalibrate_thresholds_report.json`,
  `models/training_report_*.json`
- **What they hold:** JSON reports emitted by the various training tools —
  per-model AUC / precision / feature counts / entry-threshold / dataset
  provenance. Informational; not read by any runtime code path.
- **How they're created / written to:**
  - `src/lgbm_poc/train.py:save_model()` writes `metadata.json` next to
    each model.
  - `src/lgbm_poc/evaluate.py:26` writes `metrics.json`.
  - `tools/retrain_live_features.py:676` writes
    `retrain_live_features_report.json`.
  - `tools/recalibrate_thresholds.py:147` writes
    `recalibrate_thresholds_report.json`.
  - `retrain_all.sh` reads `staging/*/metrics.json` for the AUC gate but
    doesn't preserve them outside staging.
- **Value tier:** **Regeneratable-but-slow** (same as the models
  themselves — regenerated by whatever produced them).
- **Failure profile:** **Silent, no runtime impact.** These files inform
  humans; nothing loads them at request time.
- **Backup coverage:** **None** local or off-host.
- **Restore steps:** re-run the tool that produced each.

---

## 7. Macro parquet feeds (`data/macro/*.parquet`)

- **Path (in-repo/live):** `data/macro/{gold,oil,dxy,spx,vix}.parquet`
- **What they hold:** Daily OHLCV for the five macro tickers, pulled from
  yfinance. ~50 KB each. Consumed by:
  - `predictor_server.py:fetch_gold_daily_candles()` (Gold panel + XAU/USD
    dashboard view).
  - `src/prepare_full_dataset.py` (macro features during retrain).
  - `/api/market-tickers` responses.
- **How they're created / written to:**
  - `src/fetch_macro.py` → `df.to_parquet(...)` (line 97).
  - Scheduled: `deploy/bare-metal/macro_refresh.timer` (2 min after boot,
    then hourly) running `macro_refresh.service` → `python fetch_macro.py
    --data-dir /opt/predictor/data/macro --days 730`.
  - Manually: `retrain_all.sh` step 2 as part of the retrain pipeline.
- **Value tier:** **Regen-fast.** Missing files just require a `fetch_macro.py`
  run (network-dependent, yfinance can be flaky/rate-limited — hence they
  ship pre-populated in the tarball to avoid a fresh install 503ing on the
  gold panel).
- **Failure profile:** **Loud.** `fetch_gold_daily_candles()` raises
  `HTTPException(503, "Gold macro feed unavailable")` if
  `GOLD_PARQUET_PATH` doesn't exist; the dashboard's Gold panel and
  `/api/market-tickers` show a red/error state until the next
  `macro_refresh.timer` tick or a manual re-run.
- **Backup coverage:** **None.** Self-healing on next hourly refresh.
- **Retention:** overwritten in place on every refresh (730-day window).
- **Restore steps:**
  ```bash
  sudo -u predictor /opt/predictor/.venv/bin/python \
      /opt/predictor/src/fetch_macro.py \
      --data-dir /opt/predictor/data/macro --days 730
  # Or wait ≤1 hour for the timer.
  ```
- **Note:** `data/macro.hidden/` in the repo (five parquet files, dated
  2026-07-19) is orphan — grep finds no references anywhere in code. Safe
  to remove; not covered here.

---

## 8. `.env` — secrets and per-host config

- **Path (in-repo):** `.env.example` (template only; `.env` is gitignored)
- **Path (live):** `/opt/predictor/.env` (`chmod 600`, owned by
  `predictor:predictor`)
- **What it holds:** `INTERNAL_API_TOKEN` (32-byte hex, signed enrichment
  channel), `ANTHROPIC_API_KEY` if set, `AGENT_RELAY_CMD`, threshold
  overrides for the forge scorecard, ports, model paths, etc. Loaded by
  `predictor.service`, `signal_agent.service`, `agent_relay.service`,
  `forge_scorecard.service` via `EnvironmentFile=-`.
- **How it's created / written to:**
  - `deploy/bare-metal/install.sh:100-186` writes a template on first
    install (with a fresh randomly-generated `INTERNAL_API_TOKEN` — wait,
    verify below).
  - Operator edits by hand thereafter.
- **Value tier:** **Irreplaceable in one respect** — a rotated
  `INTERNAL_API_TOKEN` breaks any active clients that cached the previous
  one. Everything else in it is regeneratable if the operator remembers
  what they set.
- **Failure profile:** **Loud.** Most services (`predictor`,
  `signal_agent`, `agent_relay`) start cleanly with a missing `.env`
  (`EnvironmentFile=-` is optional) — but degrade: `/api/chat` returns
  "not configured", signal-agent enrichment stays off, etc.
- **Backup coverage:** **None.** Not in git (correct), not snapshotted
  anywhere.
- **Off-host target:** TBD (personal cloud) — this is a real secret, needs
  encrypted-at-rest storage wherever it lands.
- **Restore steps:** hand-recreate from `install.sh`'s template + operator
  memory of what was tuned. There is currently no automated path.

---

## 9. `/etc/nginx/.htpasswd` — basic-auth credentials

- **Path (live only):** `/etc/nginx/.htpasswd`
- **What it holds:** One line, `<user>:<bcrypt-hash>`, generated by
  `install.sh:277` (`htpasswd -cb`) from a fresh random 20-char password
  (`openssl rand -base64 18 | tr -d '=+/' | head -c 20`). Printed once in
  the install log, never stored anywhere else.
- **How it's created / written to:** `install.sh` idempotent block —
  leaves an existing file alone on re-run (so shared testers' credentials
  don't silently rotate).
- **Value tier:** **Irreplaceable in effect** — the plaintext password is
  never persisted anywhere, only its bcrypt hash lands in the file. If
  the file is lost the password can't be recovered.
- **Failure profile:** **Loud** — nginx returns 401 for every dashboard
  request until reset.
- **Backup coverage:** **None.**
- **Off-host target:** TBD.
- **Restore steps:**
  ```bash
  # Force-regenerate a fresh credential (dashboard testers will need the
  # new one). Or set BASIC_AUTH_USER/BASIC_AUTH_PASS beforehand to seed.
  ENABLE_BASIC_AUTH=true bash /opt/predictor/deploy/bare-metal/install.sh
  ```

---

## 10. Scorecard human-readable dump

- **Path (in-repo):** not applicable — generated at runtime only
- **Path (live):** `/opt/predictor-forge-scorecard/scorecard.txt`
  (deliberately outside `/opt/predictor/`, per `forge_scorecard.service`
  `ReadWritePaths`)
- **What it holds:** Plain-language per-strategy verdict dump — a `cat`able
  view for the operator, no API needed. Rendered by
  `tools/forge_scorecard.py:_render_text()`.
- **How it's created / written to:** `tools/forge_scorecard.py:run_once()`
  → `dump_path.write_text(...)` on every `forge_scorecard.timer` tick
  (10 min after boot, then hourly).
- **Value tier:** **Regen-fast.** Rebuilt from `forge.db` on the next
  timer tick (≤1 h) — as long as #2 is intact, this is trivially
  regeneratable.
- **Failure profile:** ephemeral; regenerated on next tick.
- **Backup coverage:** **None needed** (regen source is backed up in #2).
- **Restore steps:**
  ```bash
  sudo -u predictor /opt/predictor/.venv/bin/python \
      /opt/predictor/tools/forge_scorecard.py
  ```

---

## 11. Service logs

- **Path (in-repo):** `logs/` (per file, generated at runtime)
- **Path (live):** `/opt/predictor/logs/predictor.log`, `backup.log`,
  `forge_backup.log`, `forge_scorecard.log`, `macro_refresh.log`,
  `admin_agent_audit.log`
- **What they hold:** service stderr/stdout via systemd's `append:...`
  redirection (see each `.service` unit's `StandardOutput=`).
  `signal_agent` writes to `journal` instead, per `signal_agent.service`.
  `admin_agent_audit.log` is the per-command audit trail for the (dev-only,
  not-shipped) `admin_agent/server.py`.
- **How they're created / written to:** systemd `append:...` for the
  file-backed units; `record()` in `admin_agent/server.py` for the audit
  log; `loguru` for library-level logging.
- **Value tier:** **Ephemeral.** Useful for post-mortem; not source of
  truth for anything a rerun couldn't reproduce.
- **Failure profile:** loud only during an active incident.
- **Backup coverage:** **None.** No rotation configured either — files
  grow unbounded (a real risk over months; not urgent today at current
  volume).
- **Retention:** none.
- **Restore steps:** n/a.

---

## 12. `.retrain_cache/`, `data/raw/`, `data/datasets/`

- **Path (in-repo):** `.retrain_cache/` (gitignored), `data/raw/`,
  `data/datasets/`
- **Path (live):** same (though `data/raw` and `data/datasets` don't ship
  in the release tarball — they're built by `retrain_all.sh` steps 1-3)
- **What they hold:**
  - `.retrain_cache/` — cached OHLCV parquet used by
    `tools/retrain_live_features.py` and
    `tools/recalibrate_thresholds.py` (~100 MB).
  - `data/raw/` — raw Bybit OHLCV / mark / funding parquet, downloaded by
    `retrain_all.sh:src/download_ohlcv.py` step 1.
  - `data/datasets/` — labeled training datasets built by
    `src/prepare_full_dataset.py` from `data/raw/`.
- **How they're created / written to:** `download_ohlcv.py`, various
  `prepare_*_dataset.py`, `retrain_live_features.py`. All write
  `.parquet` via `df.to_parquet(...)`.
- **Value tier:** **Regen-slow.** Re-downloadable from Bybit but that's
  a multi-hour hit (500k rows × 6 timeframes × 3 pairs, and Bybit
  rate-limits).
- **Failure profile:** **Loud during a retrain** (adds hours), silent
  during normal operation.
- **Backup coverage:** **None.**
- **Restore steps:** re-run `retrain_all.sh --skip-macro` (or without any
  skip flag).

---

## 13. Ad-hoc pre-retrain model backup directories

- **Path (in-repo):** `models/backup_pre_expand_20260719_164210/`,
  `models/backup_pre_h13_retrain_20260719_083506/`,
  `models/backup_pre_htf_history_expand_20260719_170000/`
- **What they hold:** Manual pre-retrain safety copies of the six model
  files, plus (in one case) `metadata.json`/`metrics.json` and a
  `config.json.bak`. Three separate directories from 2026-07-19, one per
  ad-hoc retrain attempt.
- **How they're created / written to:** Not by any shipped script —
  these were created by hand during the H-13 remediation work in July.
  `retrain_all.sh` DOES write `models/backup_${TS}/` on production
  retrains (line 276 of retrain_all.sh), but under a different filename
  convention.
- **Value tier:** **Ephemeral** — safety net that's already served its
  purpose; the models it backs up are historical, not the current
  production set. Not needed for restore of any current state.
- **Failure profile:** none — no runtime dependency.
- **Backup coverage:** none (they ARE ad-hoc backups themselves).
- **Restore steps:** n/a.

---

## 14. RAM-only ephemeral state (documented for completeness)

Not durable, cannot be backed up, listed so the audit has no gaps:

- **`AssetEngine.trades_history` and per-symbol counters** in
  `predictor_server.py` — reseeded on restart from `signal_history.db`
  via `load_history()`, so effectively durable via #1.
- **`signal_agent.main._tick()`'s `last_enriched` map** — pure in-memory
  cooldown timer. Restart resets every asset's cooldown to zero (i.e.
  the next signal above threshold will enrich immediately). Not a bug,
  by design.
- **`forge/collector.py:LiveCollector._history` per-symbol deques** —
  bounded `deque(maxlen=ATR_PERIOD + 5)`, kilobytes total. Rebuilds
  within seconds of the next WebSocket tick.
- **Nginx / systemd runtime state** — under `/run/`, `/var/lib/systemd/`,
  standard OS-managed, not this project's concern.

---

## 15. Cross-cutting gaps and observations

**Unprotected data (nothing local, nothing off-host):**

1. **Persona memory** (`logs/crypto_operator_memory.jsonl`, §3) —
   irreplaceable, silent-loss, ZERO backup. This is the most obvious next
   gap to close; the exact pattern already exists twice
   (`backup_signal_log.py`, `backup_forge_db.py`) and just needs a third
   copy for this file.
2. **Production models** (§4) — regeneratable but slow, no off-app-dir
   snapshot. `retrain_all.sh`'s own `backup_${TS}/` is INSIDE the app dir
   so it dies with the app dir. A `tools/backup_models.py` would give the
   same rm-rf resilience as the two SQLite backups.
3. **`.env`** (§8) — secrets, tunings, `INTERNAL_API_TOKEN`. Needs
   encrypted-at-rest handling wherever it eventually goes off-host.
4. **`/etc/nginx/.htpasswd`** (§9) — password unrecoverable if lost. Small
   enough to piggyback onto whatever off-host secret-sync mechanism ends
   up handling `.env`.
5. **All of `/opt/predictor-backups/` itself** (§summary row 19) — this is
   the single point where off-host sync eventually needs to land. Losing
   this directory alongside the host today means the local-backup
   protections in §1 and §2 evaporate too.

**Silent-loss failure modes** — data losses that no active healthcheck
would catch:

- Corrupted/deleted `signal_history.db`: only visible on next
  `predictor.service` restart's `load_history()` reseed to zero.
- Corrupted/deleted `forge.db`: only visible on next scorecard tick /
  API read.
- Corrupted/deleted `crypto_operator_memory.jsonl`: only visible when
  Hermes fails to recall a prior exchange the operator brings up.
- `config.json` silently overwritten with stale thresholds: no signal
  anywhere in the logs, only visible if the operator notices the fire
  pattern changed.

**Deferred / on the roadmap** (from HANDOFF §7.11 explicit out-of-scope
list, restated here so a future retrain doesn't get blindsided):

- `config.json` host-binding sed pattern is brittle under regeneration —
  a proper fix is env-var-driven config.
- `forge.Dockerfile` uses hardcoded unpinned deps instead of
  `requirements.txt` — dep drift risk on the docker deploy target only.
- Docker deploy has no scorecard scheduler (bare-metal only).
- `macro_refresh.service` still lacks
  `NoNewPrivileges`/`PrivateTmp`/`ProtectSystem`/`ReadWritePaths` — low
  blast radius on a oneshot yfinance fetch, but a gap.

**Immediate wins for the next backup/off-host/restore tag chain** (in
suggested order):

1. `tools/backup_persona_memory.py` + `persona_backup.{service,timer}`
   into the same `/opt/predictor-backups/` directory, filename prefix
   `crypto_operator_memory.*.jsonl`. Zero new patterns needed; copy the
   `backup_signal_log.py` template one-to-one.
2. `tools/backup_models.py` — dump the current 6 models into
   `/opt/predictor-backups/models-YYYYMMDD-HHMMSS.tar.gz` on a
   less-frequent cadence (daily is plenty; models change on retrain, not
   per tick). Small (~2.5 MB total for all six, gzipped).
3. Off-host sync target design. `BACKUP_DIR` (`/opt/predictor-backups/`)
   is already the correct single directory to point remote sync at; the
   scorecard dump (`/opt/predictor-forge-scorecard/`) is a second small
   directory. `.env` and `/etc/nginx/.htpasswd` need separate encrypted
   handling.
4. Log rotation for `/opt/predictor/logs/*.log` — not a backup item, but
   a real "unbounded growth" risk over months, and cheap to fix with
   `logrotate.d`.
