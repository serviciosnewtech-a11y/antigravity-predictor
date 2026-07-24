# Restore Playbook -- Antigravity Predictor

Written 2026-07-24 for beta-1.10.23 as the counterpart to the
backup/off-host tag chain that just landed (beta-1.10.20 through
beta-1.10.22). This is the "how do we bring it back from
`/opt/predictor-backups/` alone if the app dir is wiped or we're
standing up on fresh hardware?" runbook.

Companion documents (read alongside):
- `docs/DATA_INVENTORY.md` -- the ground-truth data map. Every restore
  step below references a numbered row from there.
- `HANDOFF.md` §7.15 through §7.17 -- the tag chain that produced the
  backup families this playbook restores from.

---

## 0. What's in `/opt/predictor-backups/` after any successful install

Four snapshot families land in the same directory by design (one place to
look, one directory to point off-host sync at):

| Filename glob | Source | Cadence | Retention env | Written by |
|---|---|---|---|---|
| `signal_history.<stamp>.db` | DATA_INVENTORY row 1 | 6h | `BACKUP_RETENTION_COUNT` (30) | `tools/backup_signal_log.py` |
| `forge.<stamp>.db` | row 2 | 6h | `FORGE_BACKUP_RETENTION_COUNT` (30) | `tools/backup_forge_db.py` |
| `configstate.<stamp>.tar.gz` | rows 4/5/6/7/9/10 | 12h | `CONFIGSTATE_BACKUP_RETENTION_COUNT` (30) | `tools/backup_config_and_secrets.py` |
| off-host copy | all of the above | 6h | wrapped tool's own | `tools/sync_backups_offsite.py` |

Timestamps are UTC: `YYYYMMDD-HHMMSS-<microseconds>`. Snapshots across the
four families won't line up exactly (different cadences, different boot
offsets) -- the restore tool picks the newest per-family by default, which
is almost always what you want. Use `--timestamp` for point-in-time
recovery (e.g. "roll back to just before a bad recalibrate").

---

## 1. Prerequisites (fresh VPS, nothing installed)

```bash
# 1. Get the tagged release onto the box (any recent tag; beta-1.10.23+
#    is what this playbook was written against).
curl -L https://... | tar -xz -C /tmp
# ...or scp the tarball from the release archive.
cd /tmp/antigravity-predictor-bare-metal-beta-1.10.23

# 2. Run install.sh. This creates /opt/predictor, /opt/predictor-backups,
#    /opt/predictor-forge-scorecard, the predictor system user, .venv,
#    systemd units, nginx site, ufw rules. Idempotent -- safe to re-run.
sudo bash deploy/bare-metal/install.sh

# 3. Get the backup snapshots onto the box. Copy them into
#    /opt/predictor-backups (or the alt dir if $BACKUP_DIR is overridden).
#    If you're pulling from off-host storage, do that here; the
#    restore tool then treats the local backup dir as the source of truth.
sudo mkdir -p /opt/predictor-backups
sudo rsync -av user@offsite:predictor-backups/ /opt/predictor-backups/
sudo chown -R predictor:predictor /opt/predictor-backups
```

At this point the app is running but with empty state (no signal history,
no forge trades, fresh models from the tarball). Steps 2 through 7 below
restore it to the state captured in the backup snapshots.

---

## 2. Stop the services touching the data you're about to overwrite

```bash
sudo systemctl stop predictor.service signal_agent.service
sudo systemctl stop forge_backup.timer predictor_backup.timer config_backup.timer
# forge.service if this is a Docker deployment: `docker compose stop forge`
```

Don't stop the timers themselves before restoring -- stop the timers so
they don't fire mid-restore and overwrite a snapshot you were just about
to use. `agent_relay.service` and `sync_offsite.timer` can stay running;
they don't touch the restored data.

---

## 3. Pick the snapshots to restore

Run the restore tool in dry-run mode to see what it would pick:

```bash
sudo /opt/predictor/tools/restore_from_backup.sh \
    --source-dir /opt/predictor-backups \
    --target-dir /opt/predictor \
    --dry-run
```

By default the tool picks the newest per-family. If you want point-in-time
recovery (e.g. "roll back to just before the 15:47 recalibrate"), pass
`--timestamp 20260724-153000` (any format
`YYYYMMDD-HHMMSS[-<microseconds>]`). The tool then picks the newest
snapshot at-or-before that instant in each family.

---

## 4. Apply the restore

Drop `--dry-run` to actually copy. The tool will refuse to run against a
target where `predictor.service` is active unless you pass `--force`.

```bash
sudo /opt/predictor/tools/restore_from_backup.sh \
    --source-dir /opt/predictor-backups \
    --target-dir /opt/predictor
```

What it does, in order:
1. `signal_history.<pick>.db` -> `<target>/logs/signal_history.db` (chown
   predictor:predictor, chmod 600).
2. `forge.<pick>.db` -> `<target>/forge_data/forge.db` (same chown/chmod).
3. `configstate.<pick>.tar.gz` extracted over the target dir:
   - `.env` -> `<target>/.env` (chmod 600)
   - `logs/crypto_operator_memory.jsonl` -> `<target>/logs/crypto_operator_memory.jsonl`
   - `config.json` -> `<target>/config.json`
   - `src/config.json` -> `<target>/src/config.json`
   - `models/model_*.txt` -> `<target>/models/*`
   - `models/*.json` -> `<target>/models/*`
   - `etc/nginx/.htpasswd` -> `/etc/nginx/.htpasswd` (only if it exists in
     the tarball; skipped otherwise -- htpasswd may be intentionally
     managed by a different flow).
4. Writes a receipt to `<target>/logs/restore_applied.log` with the
   selected snapshot filenames, their sha256s, and the restore
   timestamp. Audit trail against "which backup did we actually apply?"

---

## 5. Restart services and verify

```bash
sudo systemctl start predictor.service
sudo systemctl start signal_agent.service        # if SA_INFERENCE_BACKEND is enabled
sudo systemctl start predictor_backup.timer forge_backup.timer config_backup.timer

# Immediate smoke checks:
curl -sf http://127.0.0.1:18910/api/status | jq .
curl -sf http://127.0.0.1:18910/health
curl -sf http://127.0.0.1:18912/recommendations   # forge scorecard, if forge is running

# Deeper checks:
tail -50 /opt/predictor/logs/predictor.log
tail -20 /opt/predictor/logs/restore_applied.log   # the receipt
sudo -u predictor sqlite3 /opt/predictor/logs/signal_history.db \
    "SELECT COUNT(*) FROM signal_events; SELECT COUNT(*) FROM trades;"
sudo -u predictor sqlite3 /opt/predictor/forge_data/forge.db \
    "SELECT COUNT(*) FROM trades; SELECT COUNT(*) FROM strategy_registry;"
```

If `predictor.service` fails to start:
- `journalctl -u predictor -n 100`
- Most common cause: `models/model_*.txt` didn't land in the target
  (configstate tarball was missing them, or the target dir's permissions
  block the read). Verify: `ls -l /opt/predictor/models/model_*.txt`.

If the counts look wrong:
- Check the receipt at `logs/restore_applied.log` -- were the intended
  snapshots picked?
- Re-run the restore tool with `--dry-run` and inspect the per-family
  picks. If a family has no snapshots at all, the tool logs it clearly
  and skips that step (partial restore is OK).

---

## 6. Point-in-time recovery: worked example

Scenario: an operator ran a bad `recalibrate_thresholds.py` at 15:47 UTC
that wrote nonsense thresholds. You want to roll back config.json but
keep the current trade history.

```bash
# See what's available:
ls -1 /opt/predictor-backups/ | head -30

# 15:00 UTC is safely before the bad run. Pick that instant:
sudo systemctl stop predictor.service
sudo /opt/predictor/tools/restore_from_backup.sh \
    --source-dir /opt/predictor-backups \
    --target-dir /opt/predictor \
    --timestamp 20260724-150000 \
    --dry-run
# ...inspect...
sudo /opt/predictor/tools/restore_from_backup.sh \
    --source-dir /opt/predictor-backups \
    --target-dir /opt/predictor \
    --timestamp 20260724-150000
sudo systemctl start predictor.service
```

Trade-history rollback caveat: this WILL also revert `signal_history.db`
and `forge.db` to their state at 15:00, so any trades opened/closed after
that will disappear from the log. If you only want to revert config, run
the tool with `--only configstate` (see `--help`).

---

## 7. Restoring from off-host storage

Same as the fresh-VPS path (§1 step 3): pull the snapshots into
`/opt/predictor-backups/` first, then run the restore tool against that
directory. The push-side is `OFFSITE_BACKUP_CMD`; the pull-side isn't
scripted yet -- if a specific pull command becomes recurring, wire it up
next to `sync_backups_offsite.py`.

---

## 8. Verifying a backup snapshot before you'll ever need to restore

Regular drill (do this quarterly, or before any risky operation):

```bash
# Pick an arbitrary snapshot triplet and restore it into a scratch dir.
STAMP=20260724-120000
SCRATCH=$(mktemp -d)

sudo /opt/predictor/tools/restore_from_backup.sh \
    --source-dir /opt/predictor-backups \
    --target-dir "$SCRATCH" \
    --timestamp "$STAMP" \
    --force  # target isn't a real install, force past the safety check

# Verify the restored DBs open cleanly:
sqlite3 "$SCRATCH/logs/signal_history.db" \
    "SELECT COUNT(*) FROM signal_events; SELECT COUNT(*) FROM trades;"
sqlite3 "$SCRATCH/forge_data/forge.db" \
    "SELECT COUNT(*) FROM trades; SELECT COUNT(*) FROM strategy_registry;"

# Verify the restored models load:
sudo -u predictor /opt/predictor/.venv/bin/python -c "
import lightgbm as lgb
for path in ['btc_long','btc_short','eth_long','eth_short','sol_long','sol_short']:
    b = lgb.Booster(model_file=f'$SCRATCH/models/model_{path}.txt')
    print(path, 'features:', b.num_feature())
"

# Cleanup:
sudo rm -rf "$SCRATCH"
```

Do NOT run the drill against `/opt/predictor` itself -- the restore tool
refuses without `--force` when a live service is active, and rightly so.

---

## 9. What this playbook does NOT cover

- Rebuilding a full retrain pipeline from scratch. If `models/model_*.txt`
  and their metadata are ALL lost (no local backup, no off-host copy,
  no repo-committed snapshot in `models/archive/`), the recovery path is
  `bash retrain_all.sh` -- multi-hour, needs fresh Bybit + yfinance
  access. See DATA_INVENTORY §4 and `retrain_all.sh` itself.
- `.retrain_cache/`, `data/raw/`, `data/datasets/` (DATA_INVENTORY rows
  12/13/14). These are not backed up on purpose (multi-GB, regen-slow but
  possible from Bybit). Recovery is `retrain_all.sh --skip-macro` (or
  full).
- Cluster-scale HA. This is a single-host deployment; nothing here
  handles multi-host state.

---

## 10. If in doubt, dry-run

Every operation the restore tool does is prefaced with a dry-run. Print
what it would do before doing it. The tool's whole design philosophy is
"idempotent, refuses in the presence of live services, prints its
receipt" -- lean on that.
