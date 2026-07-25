# Remote Deploy Handoff — beta-1.10.23 via SSH

**Written 2026-07-24 to brief Sonnet 5 (or any agent with SSH access to a fresh
Ubuntu 22.04 VPS) on a remote deploy of Antigravity Predictor.**

Read this top-to-bottom before running anything. Do not skim — sections
"Config decisions", "Known live gotchas", and "First-deploy rehearsal" all
represent real live-deploy failures that were expensive to diagnose the
first time.

You (the agent doing the deploy) have SSH access to the VPS. The operator
(Luis) has the tarball on their local machine. The tarball is the whole
deploy artifact — no `git clone` on the VPS, no fetching from GitHub.

---

## TL;DR

1. Verify `.env` decisions with Luis before starting (below).
2. SCP the bare-metal tarball + its `.sha256` to the VPS.
3. Verify sha256, extract to `/tmp`, run `install.sh` as root.
4. Populate `.env` from your verified decisions, restart affected services.
5. Verify via `/api/status`, `/health`, `/recommendations`, and a scorecard-run smoketest.
6. Confirm to Luis, hand back with the printed basic-auth password (only printed once, at install time).

Total: ~15 minutes wall-clock if nothing goes sideways.

---

## What you're deploying

**Tag:** `beta-1.10.23`. The chain from `beta-1.10.10` through `beta-1.10.23`
is all still-unapplied on the target host (or any prior rehearsal state was
wiped per §7.5). This install lands everything at once.

**Tarball:** `antigravity-predictor-bare-metal-beta-1.10.23.tar.gz` from
`<operator-machine>:.../releases/antigravity-predictor/beta-1.10.23/`
sha256 `7d9db7fa7d59a63a099ff6494629a78686288461e13df324e9fb84f288cad0ca`.
(The docker tarball also exists in that directory but you are not using
docker for this deploy.)

**What ships in this tag chain, in one sentence each:**

- `1.10.10-1.10.14`: relay warm-up, h11 protocol fix, loopback bind, nginx basic auth, ufw firewall, signal-history durability, +10-day chart history.
- `1.10.15`: Forge scoring loop (canonical strategy IDs, plain-language verdicts, per-hour scorecard timer, apply-recommendation endpoint).
- `1.10.16`: housekeeping bundle (pytest.importorskip guards, `Group=predictor` on all services, `forge.db` backup timer, venv verification in install.sh).
- `1.10.17-19`: dashboard polish (full 1000-candle history, split-chart toggle, sidebar scroll cue).
- `1.10.20`: repo housekeeping (docs archived, session artifacts pruned).
- `1.10.21`: `configstate_backup.timer` — snapshots `.env`, `config.json`, `models/*`, `/etc/nginx/.htpasswd`, and persona memory every 12h.
- `1.10.22`: `sync_offsite.timer` — env-var-driven off-host sync stub. Ships disabled by default; opts in when `OFFSITE_BACKUP_CMD` is set in `.env`.
- `1.10.23`: `tools/restore_from_backup.sh` + `docs/RESTORE_PLAYBOOK.md`.

See `HANDOFF.md` §7.6-§7.18 in the tarball for the full narrative if any
step fails and you need to understand *why* something was done a certain way.

---

## Prerequisites on the VPS

- **OS:** Ubuntu 22.04 LTS (other Debian-family versions may work; not tested).
- **Access:** root, or sudo to root. `install.sh` requires root (`[[ $EUID -eq 0 ]] || die`).
- **Disk:** ≥ 4 GB free in `/opt` (venv is ~1 GB with lightgbm, `data/macro` fetches parquet, backups accumulate).
- **Network:** outbound HTTPS to Bybit, yfinance, and PyPI. Inbound 22/80/443 only (install.sh configures `ufw` for exactly that).
- **DNS:** if you want HTTPS post-deploy, point a domain at the VPS's IP BEFORE running certbot. Not needed for install.sh itself.

---

## Config decisions — get these from Luis BEFORE starting

`install.sh` writes a template `.env`; you'll edit it after install with these values. Don't guess any of them — ask.

| Var | What it is | Ask Luis for |
|---|---|---|
| `ANTHROPIC_API_KEY` | For `signal_agent`'s enrichment path. Optional if using local Hermes relay only. | Value or "leave unset" |
| `AGENT_RELAY_CMD` | Shell template for the local CLI-agent that backs `/api/chat`. `{prompt}` placeholder. Must be absolute path. See HANDOFF §4 and `LIVE_DEPLOY_NOTES.md` #7. | The exact string. If Luis says "leave unconfigured," that's fine — `/api/chat` will return 503 until it's set (see §7.5). |
| `SA_INFERENCE_BACKEND` | `enabled` or `disabled`. Governs whether the signal-agent auto-enrichment runs. | Value |
| `ENABLE_BASIC_AUTH` | `true` or `false`. If true, nginx demands a password for the dashboard. Default `true`. | Confirm (recommended: `true` for any internet-exposed host). |
| `OFFSITE_BACKUP_CMD` | Env-var-driven off-site push (see §7.17). Personal cloud not chosen yet per Luis. **Leave unset for this deploy**; you'll come back to it once his cloud is picked. | Confirm "leave unset for now" |
| Domain for HTTPS | If Luis wants HTTPS today, need the domain name to run `certbot --nginx -d <domain>` post-install. | Domain or "HTTP only for now" |

If Luis says "just use safe defaults where possible" — use them, but ask
about `AGENT_RELAY_CMD` explicitly. That one has no sane default; a wrong
value silently breaks `/api/chat` and eats debugging time (§7.5).

---

## Deploy sequence

### Step 1: transfer + verify tarball

From Luis's machine (he does this, not you):
```bash
scp ../releases/antigravity-predictor/beta-1.10.23/antigravity-predictor-bare-metal-beta-1.10.23.tar.gz \
    ../releases/antigravity-predictor/beta-1.10.23/antigravity-predictor-bare-metal-beta-1.10.23.tar.gz.sha256 \
    root@<vps>:/tmp/
```

On the VPS (you do this):
```bash
cd /tmp
sha256sum -c antigravity-predictor-bare-metal-beta-1.10.23.tar.gz.sha256
# Expect: "OK". If it fails, STOP — do not proceed with a corrupt tarball.
```

### Step 2: extract + inspect before executing anything

```bash
tar -xzf antigravity-predictor-bare-metal-beta-1.10.23.tar.gz
cd antigravity-predictor-bare-metal-beta-1.10.23
ls -la deploy/bare-metal/install.sh   # exists, +x
head -30 deploy/bare-metal/install.sh # sanity: script header matches beta-1.10.16-1.10.23 comments
```

### Step 3: run install.sh

```bash
# Default install (/opt/predictor as user 'predictor'):
sudo bash deploy/bare-metal/install.sh 2>&1 | tee /tmp/install.log

# Custom install path/user — flags OR env vars, either works (beta-1.10.24):
sudo bash deploy/bare-metal/install.sh --app-dir /home/luis/antigravity-predictor --user luis 2>&1 | tee /tmp/install.log
# equivalently:
APP_DIR=/home/luis/antigravity-predictor APP_USER=luis sudo -E bash deploy/bare-metal/install.sh 2>&1 | tee /tmp/install.log
# (the -E on the second form matters — without it, sudo strips APP_DIR/APP_USER
#  from the child env and you get the defaults regardless of what you exported)

# Prior to beta-1.10.24 the flag form silently ignored --app-dir/--user and
# installed to /opt/predictor as predictor regardless. If you see that
# behavior, you're on an old tarball — check `bash install.sh --help`.
```

**Watch for these lines in the output — they matter:**

- `[INSTALL] Setting up Python venv…` followed by no `[ERROR] venv creation failed`. If it errors here, see "Known live gotchas" below — §7.7's silent venv failure has an actionable message now (beta-1.10.16) but no root-cause fix.
- `[INSTALL] Python deps installed.` — pip finished cleanly.
- `[INSTALL] Basic auth enabled — user: predictor password: <XXX>` — **capture this password immediately.** It's printed once, then only exists in `/etc/nginx/.htpasswd` as a bcrypt hash. If you miss it and Luis needs it later, you'll have to regenerate via `htpasswd /etc/nginx/.htpasswd predictor` and let him know it changed.
- `[INSTALL] Services enabled.` — systemd knows about them.
- `[INSTALL] Antigravity Predictor installed successfully.` — the closing banner. If you don't see this, something failed silently in the last section; check exit code.

**Timers enabled by this install** (from beta-1.10.15-1.10.22):
`predictor_backup.timer` (6h), `forge_backup.timer` (6h),
`forge_scorecard.timer` (1h), `config_backup.timer` (12h),
`sync_offsite.timer` (6h, degrades when unconfigured),
`macro_refresh.timer` (1h).

### Step 4: populate `.env`

```bash
nano /opt/predictor/.env
# Set the values Luis gave you in "Config decisions" above.
# ALL variables have documentation comments in the template — read them.
chown predictor:predictor /opt/predictor/.env
chmod 600 /opt/predictor/.env
```

Then restart services that read `.env`:
```bash
systemctl restart predictor agent_relay
# signal_agent only if SA_INFERENCE_BACKEND=enabled
[[ "$(grep '^SA_INFERENCE_BACKEND=' /opt/predictor/.env | cut -d= -f2)" == "enabled" ]] && \
    systemctl restart signal_agent
```

### Step 5: HTTPS (if Luis provided a domain)

```bash
certbot --nginx -d <domain> -n --agree-tos -m <email>
# Follow prompts. certbot rewrites /etc/nginx/sites-available/predictor,
# reloads nginx, sets up auto-renewal via its own timer.
```

If HTTP-only for now, skip. Luis can run certbot himself later.

---

## Post-deploy verification

Run each command, confirm each expected output. **Do not report "deployed" to Luis until all seven pass.**

```bash
# 1. Predictor process alive + serving
systemctl status predictor --no-pager | head -12
# Expect: Active: active (running), no crash-loops.

# 2. Predictor API responding, bound to loopback (not public interface)
curl -s http://127.0.0.1:18910/api/status | jq -r .status
# Expect: "online" (or similar; not "error", not connection refused).
ss -ltnp | grep :18910
# Expect: 127.0.0.1:18910 (NOT 0.0.0.0:18910). If 0.0.0.0, the config.json
# sed rewrite in install.sh didn't fire — investigate before continuing.

# 3. Nginx serving the dashboard through basic auth
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/
# Expect: 401 (auth wall works). If 200 without a password, ENABLE_BASIC_AUTH
# was false or nginx conf wasn't patched — check /etc/nginx/sites-available/predictor.
curl -s -o /dev/null -w "%{http_code}\n" -u "predictor:<password-from-step-3>" http://127.0.0.1/
# Expect: 200 (dashboard reachable with password).

# 4. Firewall dropped non-allowed ports
ufw status verbose | head -15
# Expect: 22, 80, 443 allowed; everything else denied.

# 5. Forge subsystem alive + scorecard timer scheduled
systemctl list-timers forge_scorecard.timer forge_backup.timer \
    predictor_backup.timer config_backup.timer sync_offsite.timer \
    macro_refresh.timer --no-pager
# Expect: all five listed with a NEXT scheduled fire time.

# 6. Run one scorecard pass manually to confirm the whole loop works
sudo -u predictor /opt/predictor/.venv/bin/python \
    /opt/predictor/tools/forge_scorecard.py
# Expect: "[forge_scorecard] Scored N strategies, dump -> /opt/predictor-forge-scorecard/scorecard.txt"
# On a fresh install N=16, all "not_enough_data" (no trades yet — that's correct, not a bug).
cat /opt/predictor-forge-scorecard/scorecard.txt | head -6
# Expect: header + "Not enough data yet (16)" section.

# 7. Run one backup of each family to confirm all four backup scripts work
sudo -u predictor /opt/predictor/.venv/bin/python /opt/predictor/tools/backup_signal_log.py
sudo -u predictor /opt/predictor/.venv/bin/python /opt/predictor/tools/backup_forge_db.py
sudo -u predictor /opt/predictor/.venv/bin/python /opt/predictor/tools/backup_config_and_secrets.py
sudo -u predictor /opt/predictor/.venv/bin/python /opt/predictor/tools/sync_backups_offsite.py
# Expect: first three write snapshots to /opt/predictor-backups/. The fourth
# logs "not configured, skipping" and exits 0 (since OFFSITE_BACKUP_CMD is unset).
ls -la /opt/predictor-backups/
# Expect: signal_history.*.db, forge.*.db, configstate.*.tar.gz — one of each.
```

---

## What to report back to Luis

- Basic-auth password from step 3 (assume it's the ONLY copy, treat accordingly).
- Which decisions from the config table are set to what values in the live `.env`.
- Any warnings that appeared during install (nothing should be fatal, but note anything unusual).
- All 7 verification checks passed / any that didn't with the actual output.
- Timer schedule from step 5 output.
- If HTTPS was configured, the domain + expiry date from `certbot certificates`.

---

## Known live gotchas (all seen and named in prior deploys)

Read these BEFORE they bite you.

### `venv creation failed` at install.sh step 3

§7.7 open item. Root cause never pinned down; beta-1.10.16 added a fast-fail so it aborts install with an actionable message instead of leaving `predictor.service` to 203/EXEC crash-loop later. If it triggers:
1. Capture the previous ~20 lines of `install.sh` output (was it actually creating the venv, or did it silently stop earlier?).
2. Check `df -h /opt` (disk full?), `apt list --installed | grep python3-venv` (installed?), and `ls -la /opt/predictor` (permissions sane?).
3. Recreate by hand: `python3 -m venv /opt/predictor/.venv && /opt/predictor/.venv/bin/pip install -r /opt/predictor/requirements.txt && chown -R predictor:predictor /opt/predictor/.venv`.
4. Re-run `install.sh` — it's idempotent for everything except the venv step (which now fails early rather than continuing).

### `/opt/predictor` is `0700` and blocks external tooling

§7.7's second friction point. `predictor`'s home dir is private, so monitoring tools running as other accounts can't `tail /opt/predictor/logs/predictor.log`. Two workarounds:
- `setfacl -Rm u:<monitor-user>:rX /opt/predictor/logs` — scoped grant.
- Or switch to `journalctl -u predictor -f` for log inspection (systemd-journal group access).
Not a bug in the app, real friction between "private home dir" and "someone needs to look in."

### `/api/chat` returns 503

Expected if `AGENT_RELAY_CMD` isn't set to something that actually works on this host. See HANDOFF §7.5 + `LIVE_DEPLOY_NOTES.md` #7. Verify by hand FIRST with `sudo -u predictor /opt/predictor/.venv/bin/python /opt/predictor/tools/agent_chat_relay.py` and `curl http://127.0.0.1:8645/health` before restarting agent_relay.service.

### First install of the day is slower than subsequent

lightgbm wheel download from PyPI is the slow part (~300 MB of deps total). Not a bug — just don't panic if `pip install` takes 3-5 minutes on a slow network.

---

## Rollback procedure

If the deploy is broken and needs to go away entirely:

```bash
systemctl stop predictor agent_relay signal_agent forge \
    predictor_backup.timer forge_backup.timer forge_scorecard.timer \
    config_backup.timer sync_offsite.timer macro_refresh.timer
systemctl disable predictor agent_relay signal_agent forge \
    predictor_backup.timer forge_backup.timer forge_scorecard.timer \
    config_backup.timer sync_offsite.timer macro_refresh.timer
rm -f /etc/systemd/system/{predictor,agent_relay,signal_agent,forge,predictor_backup,forge_backup,forge_scorecard,config_backup,sync_offsite,macro_refresh}.{service,timer}
systemctl daemon-reload
rm -rf /opt/predictor
# Keep /opt/predictor-backups (that's your data — the whole point).
# Keep /etc/nginx/.htpasswd unless Luis says otherwise.
```

If the deploy is broken but recoverable:
- Individual service log inspection: `journalctl -u <service> -e --no-pager | tail -50`.
- Full install log: `/tmp/install.log` (from step 3).
- Predictor app log: `tail -100 /opt/predictor/logs/predictor.log`.

Report the actual failure to Luis before doing anything destructive.

---

## Restoring from a previous host's backups (partial-loss scenario)

If Luis has `/opt/predictor-backups/` tarballs from a prior host and wants to
carry the data forward instead of starting empty:

1. Fresh install as above (steps 1-4).
2. Stop the services: `systemctl stop predictor forge`.
3. Copy the backup tarball into place: `scp <old-host>:/opt/predictor-backups/*.tar.gz /opt/predictor-backups/` (or download from off-site if configured).
4. Run `sudo -u predictor /opt/predictor/tools/restore_from_backup.sh --source-dir /opt/predictor-backups --target-dir /opt/predictor --timestamp <YYYYMMDD-HHMMSS>` (see `docs/RESTORE_PLAYBOOK.md` in the tarball for the full procedure with dry-run + parity verification steps).
5. Start services back up: `systemctl start predictor forge`.
6. Verify: `curl 127.0.0.1:18912/recommendations | jq '.count'` should match what the source host had.

---

## Where to look for deeper context if you need to understand *why*

Inside the extracted tarball at `/tmp/antigravity-predictor-bare-metal-beta-1.10.23/`:

- `HANDOFF.md` — running narrative. §2 for tag currency, §7.6-§7.18 for the recent chain, §5 for release conventions.
- `docs/DATA_INVENTORY.md` — every piece of durable data, its backup coverage, restore steps.
- `docs/RESTORE_PLAYBOOK.md` — the runbook for the "wipe + carry data forward" flow above.
- `deploy/bare-metal/LIVE_DEPLOY_NOTES.md` — 7 numbered gotchas from prior live deploys. Read at least #7 (AGENT_RELAY_CMD trap) before touching agent_relay.
- `../releases/README.md` (outside the tarball, on Luis's machine) — the ship log for every prior tag.

Do NOT `git clone` the repo on the VPS. This tarball IS the deploy. The
distinction matters: `install.sh` computes its `REPO_SRC` relative to its
own location, and the packaged tree has intentionally stripped bits
(admin_agent, deploy/docker, run_*.sh in some packages) that a plain
clone wouldn't have.

---

## Constraints on you (the deploying agent)

- Don't invent env-var values. Ask.
- Don't skip verification. Post-deploy claims of "shipped" without evidence have caused real regressions on this project.
- Don't `git clone` on the VPS. Use the tarball.
- If a step fails, STOP and report to Luis. Don't chain retries — beta-1.10.15's initial ship had a version-in-git-tags-vs-tarball drift that took real effort to unwind because retries stacked.
- The full 90+ pytest suite passes against this tarball on any host with Python 3.10+ and requirements.txt installed. If Luis wants pre-deploy confidence, run `PYTHONPATH=$PWD python3 -m pytest tests/` from inside the extracted tarball on the VPS (or your own machine); should be all-green.
