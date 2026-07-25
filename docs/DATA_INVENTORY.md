# Data Inventory -- Antigravity Predictor

Audit refreshed 2026-07-25 against `beta-1.10.28` (HEAD `f542dc8`). Supersedes
the earlier version keyed to `beta-1.10.19`; the backup / off-host / restore
work in `beta-1.10.21/22/23` (HANDOFF §7.16-§7.18) changed which rows are
still exposed. Companion to `HANDOFF.md`. Where they disagree, verify against
code -- HANDOFF is a running narrative, this is a point-in-time audit.

**Axes**

- **Irreplaceable** = cannot be recomputed from other on-disk state.
  **Regenerable-slow** = rebuildable but expensive (hours of retraining
  or slow re-download). **Ephemeral** = fine to lose.
- **Protected** = at least one automated timer snapshots it into
  `/opt/predictor-backups/`. **Exposed** = single copy, no snapshot.
- **Off-host** everywhere = **not yet wired.** `sync_offsite.timer` is
  installed and enabled but `OFFSITE_BACKUP_CMD` is empty by default; the
  service exits 0 with a `not configured, skipping` line until Luis picks a
  destination. Every "Protected" row below is local-disk redundancy only --
  it defends against `rm -rf /opt/predictor` or a bad `install.sh` re-run,
  NOT against loss of the whole host/disk.

Repo-relative paths are given first; on the live host they resolve under
`/opt/predictor/...` (bare-metal) or the container volume (Docker).

---

## 0. Summary table

| # | Source | Live path | Class | Coverage | Restore difficulty |
|---|---|---|---|---|---|
| 1 | `signal_history.db` | `/opt/predictor/logs/signal_history.db` | Irreplaceable | **Protected** -- `predictor_backup.timer` 6h -> `signal_history.*.db` | Trivial -- `cp` newest snapshot |
| 2 | `forge.db` (trades, candles, registry, scorecard, evaluation_history) | `/opt/predictor/forge_data/forge.db` | Irreplaceable (trades + eval_history); rolling (candles) | **Protected** -- `forge_backup.timer` 6h -> `forge.*.db` | Trivial -- `cp` newest snapshot |
| 3 | Persona memory (`crypto_operator_memory.jsonl`) | `/opt/predictor/logs/crypto_operator_memory.jsonl` | Irreplaceable | **Protected** -- inside `configstate.*.tar.gz`, 12h | Extract from tarball |
| 4 | Production LightGBM models (6 x `.txt`) | `/opt/predictor/models/model_*.txt` | Regen-slow (multi-hour retrain) | **Protected** -- inside `configstate.*.tar.gz`, 12h | Extract from tarball; else re-retrain |
| 5 | `config.json` + `src/config.json` copy | `/opt/predictor/config.json`, `.../src/config.json` | Regen-slow (recalibrate_thresholds) | **Protected** -- inside `configstate.*.tar.gz`, 12h | Extract from tarball; else re-run recalibrate |
| 6 | Model metadata / metrics / retrain / recalibrate reports (`models/*.json`) | `/opt/predictor/models/*.json` | Regen-slow | **Protected** -- inside `configstate.*.tar.gz`, 12h | Extract or re-emit from training tool |
| 7 | `.env` (`INTERNAL_API_TOKEN`, API keys, tunings) | `/opt/predictor/.env` | Irreplaceable (token if never re-derivable elsewhere) | **Protected LOCAL only** -- inside `configstate.*.tar.gz`, 12h. **Not encrypted at rest.** Off-host would leak plaintext secrets. | Extract from tarball; else hand-recreate |
| 8 | `/etc/nginx/.htpasswd` (bcrypt hash) | `/etc/nginx/.htpasswd` | Irreplaceable (plaintext password printed once during install, never persisted) | **Protected** -- inside `configstate.*.tar.gz`, 12h | Extract; else force-regenerate via `install.sh` (rotates testers' password) |
| 9 | Macro parquet feeds (gold/oil/dxy/spx/vix) | `/opt/predictor/data/macro/*.parquet` | Regen-fast (self-healing) | Exposed -- refreshed hourly by `macro_refresh.timer` from yfinance | n/a, self-heals in <=1h |
| 10 | Scorecard human-readable dump | `/opt/predictor-forge-scorecard/scorecard.txt` | Regen-fast (rebuilt from #2) | Not backed up (source #2 is) | Regen -- runs on `forge_scorecard.timer`, <=1h |
| 11 | `/opt/predictor-backups/` (all snapshots) | `/opt/predictor-backups/` | Derived from #1/#2/#3-#8 | **Off-host: unconfigured.** `sync_offsite.timer` mechanism exists (§7.17) but `OFFSITE_BACKUP_CMD` empty. Whole disaster-recovery story dies here if the host is lost. | n/a -- this IS the restore source |
| 12 | Service log files (`predictor.log`, `backup.log`, `forge_backup.log`, `config_backup.log`, `forge_scorecard.log`, `macro_refresh.log`, `admin_agent_audit.log`) | `/opt/predictor/logs/*.log` | Ephemeral (post-mortem only) | Exposed -- no rotation, unbounded growth | n/a |
| 13 | `.retrain_cache/*.parquet` (cached Bybit OHLCV) | `/opt/predictor/.retrain_cache/` (~100 MB on dev box) | Regen-slow (multi-hour Bybit re-fetch) | Exposed (git-ignored, dev-only) | Re-run `retrain_all.sh` |
| 14 | `data/raw/*.parquet` + `data/datasets/*.parquet` (retrain inputs) | `/opt/predictor/data/raw/`, `/opt/predictor/data/datasets/` (multi-GB during retrain) | Regen-slow | Exposed | Re-run `retrain_all.sh` |
| 15 | RAM-only ephemeral (see §4) | -- | Ephemeral by design | n/a | n/a |

---

## 1. Irreplaceable + Protected

Everything here has a timer writing to `/opt/predictor-backups/`. Local-disk
only until off-host sync is wired.

### 1.1 `signal_history.db` (row 1)

- **Purpose:** every signal transition + every simulated trade the predictor
  has ever emitted. Substrate for eventual threshold retuning.
- **Size class:** KB->MB over months (currently ~empty on dev box, 80 KB
  typical on a running host after a few days).
- **Write frequency:** per tick (~4/min per symbol) + per closed trade.
- **Backup:** `tools/backup_signal_log.py` via
  `predictor_backup.service`/`.timer` (SQLite online backup API into
  `signal_history.YYYYMMDD-HHMMSS-uuuuuu.db`). Fires 5 min after boot then
  every 6h. Retention `BACKUP_RETENTION_COUNT` (default 30).
- **Silent-loss?** Yes -- only visible after next `predictor.service`
  restart when `AssetEngine.load_history()` reseeds counters to zero.
- **Restore:** covered by `tools/restore_from_backup.sh` (`--only
  signal_history`) or the manual `cp` recipe in
  `docs/RESTORE_PLAYBOOK.md`.

### 1.2 `forge.db` (row 2)

- **Purpose:** paper-trading history (trades, candles, canonical strategy
  registry, scorecard, append-only evaluation history).
- **Size class:** MB range on an active host (candles pruned per-symbol
  to 5000 rows; trades unbounded).
- **Write frequency:** per tick (candles) + per closed simulated trade +
  per scorecard run.
- **Backup:** `tools/backup_forge_db.py` via `forge_backup.timer`. Same
  design as row 1, filename prefix `forge.*.db`, same directory. 6h cadence.
  Retention `FORGE_BACKUP_RETENTION_COUNT` (default 30).
- **Silent-loss?** Yes -- next scorecard run repopulates against zero
  history, every strategy flags `not_enough_data`.
- **Restore:** `tools/restore_from_backup.sh --only forge`.

### 1.3 Persona / Hermes shared memory (row 3)

- **Purpose:** append-only JSONL log of every operator <-> Hermes exchange
  and every automated enrichment digest. Recalled into the system prompt
  on each new call.
- **Size class:** KB per month (small; capped at 2000 lines by
  `hermes_persona.memory_append()`).
- **Write frequency:** per chat message + per signal-enrichment event
  (best-effort; write failures don't break the caller).
- **Backup:** bundled into `configstate.*.tar.gz` by
  `tools/backup_config_and_secrets.py` on `config_backup.timer` (12h,
  starts 9 min after boot). Retention
  `CONFIGSTATE_BACKUP_RETENTION_COUNT` (default 30).
- **Silent-loss?** Yes -- `memory_recall()` returns empty string on a
  missing file, no warning; operator only notices when Hermes "forgets"
  a prior exchange.
- **Restore:** `tools/restore_from_backup.sh --only configstate` extracts
  the tarball; the file lands back at `logs/crypto_operator_memory.jsonl`.

### 1.4 `.env` (row 7)

- **Purpose:** all secrets and per-host tuning (`INTERNAL_API_TOKEN`,
  optional `ANTHROPIC_API_KEY`, `AGENT_RELAY_CMD`, threshold overrides,
  `BASIC_AUTH_*` if set, `OFFSITE_BACKUP_CMD` if configured).
- **Size class:** ~10 KB.
- **Write frequency:** hand-edited by operator; `install.sh` writes a
  template on first install.
- **Backup:** inside `configstate.*.tar.gz`, 12h. **Warning:** the
  tarball is not encrypted. Off-host push, once wired, will move
  plaintext secrets unless `OFFSITE_BACKUP_CMD` wraps them in
  `age`/`rclone crypt`/etc. -- see §7.17 in HANDOFF for the deliberate
  design gap.
- **Silent-loss?** Mostly loud (services degrade visibly) but individual
  key rotations (e.g. lost `INTERNAL_API_TOKEN`) fail silently against
  any client still holding the old value.
- **Restore:** extract from tarball; else hand-recreate from
  `install.sh` template + operator memory.

### 1.5 `/etc/nginx/.htpasswd` (row 8)

- **Purpose:** basic-auth credential for the public dashboard.
  Bcrypt-hashed; plaintext printed once in install log, never re-derivable
  from the hash.
- **Size class:** <1 KB.
- **Write frequency:** once at install (`install.sh` is idempotent -- keeps
  existing password across re-runs).
- **Backup:** inside `configstate.*.tar.gz`, 12h. `config_backup.service`
  has `ReadOnlyPaths=/etc/nginx/.htpasswd` to reach it despite
  `ProtectSystem=strict`.
- **Silent-loss?** Loud -- nginx 401s every request.
- **Restore:** `tools/restore_from_backup.sh --only configstate` (if run
  as root; test drills that aren't root will skip this file with a
  clear log line and note in the receipt); else force-regenerate via
  `ENABLE_BASIC_AUTH=true bash install.sh` (rotates the password --
  every tester needs the new one).

---

## 2. Irreplaceable + NOT Protected -- the risk list

**Nothing in the current repo tree is unprotected-and-irreplaceable.** The
`beta-1.10.21` configstate bundle closed the persona-memory / `.env` /
`.htpasswd` gaps that the earlier audit flagged. What remains is a
**second-order** risk:

### 2.1 `/opt/predictor-backups/` itself has no off-host copy (row 11)

- **Path:** `/opt/predictor-backups/` (live host only; not in repo).
- **Purpose:** single directory holding every snapshot family
  (`signal_history.*.db`, `forge.*.db`, `configstate.*.tar.gz`).
- **Coverage:** `sync_offsite.timer` and `tools/sync_backups_offsite.py`
  exist (beta-1.10.22, §7.17) but `OFFSITE_BACKUP_CMD` is empty by
  default. Until Luis picks a destination, host-loss = all-snapshots-loss.
- **What "wiring it" means:** set `OFFSITE_BACKUP_CMD` in `.env` to
  something like `rclone copy $BACKUP_DIR mydrive:predictor-backups` or
  `rsync -av $BACKUP_DIR user@host:...`, `systemctl restart
  sync_offsite.timer`, watch the first push. Design deliberately keeps
  the destination choice out of code.

---

## 3. Regenerable but slow

Cost-of-loss is measured in hours (retrain time, re-download time), not
data value. None of these are backed up; all are reproducible from
`retrain_all.sh` + Bybit + yfinance.

### 3.1 LightGBM models (row 4)

- **Path (repo/live):** `models/model_{btc,eth,sol}_{long,short}.txt`
  (6 files, ~2.5 MB total).
- **Regen path:** `retrain_all.sh` (multi-hour) writes staging copies,
  step 6 promotes the ones clearing `MIN_AUC=0.54` into `models/`.
- **Failure profile:** loud at next `predictor.service` restart --
  missing model raises on `AssetEngine.load_models()`.
- **Note:** now ALSO inside `configstate.*.tar.gz` (row 4 above), so
  in practice this is protected as long as backups are reachable. Full
  retrain remains the ultimate fallback.

### 3.2 `config.json` (row 5)

- **Regen path:** `tools/recalibrate_thresholds.py --fire-rate 0.65`.
  Fast if models + `.retrain_cache/` are already on disk.
- **Silent-loss** if overwritten with stale thresholds -- the predictor
  loads it and fires at the wrong entry probs, no log signal until an
  operator notices.

### 3.3 Model metadata / metrics / reports (row 6)

- Purely informational -- no runtime code loads them.
- Regen path: re-run whichever training tool produced each
  (`src/lgbm_poc/train.py` for `metadata.json`,
  `src/lgbm_poc/evaluate.py` for `metrics.json`,
  `tools/retrain_live_features.py` for
  `retrain_live_features_report.json`, etc.).

### 3.4 `.retrain_cache/`, `data/raw/`, `data/datasets/` (rows 13, 14)

- Cached / downloaded intermediates for the retrain pipeline. Multi-GB.
- Excluded from `configstate.*.tar.gz` deliberately -- too big, and
  Bybit will re-serve them. If the retrain cache is gone during a
  retrain, add hours. During normal operation, invisible.

---

## 4. Ephemeral

Fine to lose; listed for completeness.

- **Service logs** (row 12) -- `predictor.log`, `backup.log`,
  `forge_backup.log`, `config_backup.log`, `forge_scorecard.log`,
  `macro_refresh.log`, `admin_agent_audit.log`. Systemd `append:...`
  redirection; no `logrotate` configured. Unbounded growth is a real
  months-scale risk; not urgent at current volume.
- **`logs/tutor_memory.jsonl`** -- artifact of the removed
  `/api/tutor-chat` endpoint (merged into `/api/chat` 2026-07-23;
  see `src/predictor_server.py:1349` comment). Nothing writes to it
  now. Flagged in `CLUTTER_ASSESSMENT.md` as delete.
- **Scorecard human-readable dump** (row 10) --
  `/opt/predictor-forge-scorecard/scorecard.txt`. Rebuilt every scorecard
  tick from `forge.db`. As long as row 2 is intact, this is free.
- **RAM-only** (row 15):
  - `AssetEngine.trades_history` / per-symbol counters -- reseeded from
    row 1 on startup, so durable-via-#1.
  - `signal_agent.main._tick()`'s `last_enriched` cooldown map -- reset
    every restart, by design.
  - `forge/collector.py` per-symbol candle deques (`maxlen=ATR_PERIOD+5`)
    -- rebuilt within seconds of the next WebSocket tick.

---

## 5. Deployed-host-only paths (not in repo)

Flagged so a "what's on the host?" audit doesn't miss them.

| Host path | What lives there | Owner | In backup? |
|---|---|---|---|
| `/opt/predictor/` | Extracted app tarball, `0700 predictor:predictor` | predictor | source of most backup contents |
| `/opt/predictor/.venv/` | Python 3.10+ venv, ~700 MB | predictor | No -- rebuilt by `install.sh` |
| `/opt/predictor-backups/` | All snapshots (rows 1, 2, and configstate) | predictor | No -- **this IS the backup dir** |
| `/opt/predictor-forge-scorecard/` | `scorecard.txt` (regen-fast) | predictor | No -- source is #2 |
| `/opt/predictor-metis/` | Isolated Hermes CLI install for `/api/chat` relay (Nous Research Hermes Agent, per HANDOFF §4). Has its own `~/.hermes/.env`. | dedicated relay user (NOT `predictor`) | No -- external product; user manages separately |
| `/etc/systemd/system/{predictor,agent_relay,signal_agent,macro_refresh,predictor_backup,forge_backup,config_backup,forge_scorecard,sync_offsite}.{service,timer}` | Copies of the units in `deploy/bare-metal/` | root | Rewritten from repo on every `install.sh` |
| `/etc/nginx/sites-{available,enabled}/predictor` | Rendered nginx config for the dashboard | root | Rewritten from `deploy/bare-metal/nginx.conf` on every `install.sh` |
| `/etc/nginx/.htpasswd` | Row 8 -- basic-auth hash | root | Yes -- via configstate |
| `/etc/sudoers.d/hermes-deploy` | NOPASSWD sudoers rule for the Hermes deploy runtime (see `docs/DEPLOY_NONINTERACTIVE.md`) | root | No -- one-line file, easily recreated from docs |
| `/tmp/deploy-<TAG>-report.txt`, `/tmp/deploy-<TAG>-<pid>.log` | Deploy-run report + tee'd log; survives terminal close | root | No -- ephemeral post-deploy artifact |

---

## 6. Open questions (Luis's call)

1. **Off-host destination** (§2.1) -- until picked, the whole backup story
   is one-disk-failure away from zero.
2. **`.env` at rest** -- once off-host lands, do we push the tarball as-is
   (relying on server-side encryption at the destination) or wrap the push
   command in `age`/`gpg`/`rclone crypt`? HANDOFF §7.17 punts this to
   configuration; may deserve a first-class doc when the destination is
   chosen.
3. **`logs/*.log` rotation** -- unbounded today. `logrotate.d` config would
   be five minutes of work; not on any chain yet.
4. **`models/archive/`** (repo-tracked, ~5 MB across three
   `backup_pre_*/` subdirs) -- kept per HANDOFF §7.15 as an archive of the
   H-13 remediation. `CLUTTER_ASSESSMENT.md` proposes marking it delete
   candidate once Luis confirms no ongoing reference.

---

## 7. Gap list -- top 5 by risk (impact x ease of loss)

1. **Off-host sync is unconfigured** (row 11). Single VPS failure loses
   the entire snapshot history alongside the primary data. Highest impact,
   easiest to trigger. Ready to close the moment Luis names a destination.
2. **`.env` inside `configstate.*.tar.gz` is unencrypted**. Once off-host
   pushes start, plaintext `INTERNAL_API_TOKEN` and any live
   `ANTHROPIC_API_KEY` land wherever the tarball lands. Mitigation is a
   `.env` decision, not a code decision.
3. **No test that `configstate.*.tar.gz` actually contains what it
   claims after a live install.** Regression tests exist for the tool
   (`tests/test_backup_config_and_secrets.py`), but a scheduled
   "verify a real snapshot round-trips" check on the live host doesn't.
   `docs/RESTORE_PLAYBOOK.md` §8 describes the drill; not automated.
4. **`/opt/predictor/.venv/` disappearing silently is still an
   unresolved failure mode** (HANDOFF §7.7). `beta-1.10.16` added a
   `[[ -x .venv/bin/python ]]` install-time check but the underlying
   cause was never found. Not a data-loss risk per se, but any restore
   drill that reaches "re-run `install.sh`" could stub its toe here.
5. **Unbounded log growth** (row 12). No `logrotate.d` config. Months-scale
   disk-fill risk, invisible until it bites.
