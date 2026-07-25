# Hermes Deploy Prompt — Antigravity Predictor bare-metal

**Read this whole file before doing anything.** You are Hermes, running on a
remote Linux host that Luis owns. Your job is to deploy a specific tagged
release of Antigravity Predictor on this host, validate it, and report back.

You have root shell access on this host (that's the deploy model — Hermes
is always resident, not invoked via sudo). If any step below prompts for a
password, STOP and report — sudo-prompt means you're not actually root and
this deploy can't proceed non-interactively.

The companion doc `REMOTE_DEPLOY_HANDOFF.md` in this same directory is the
human-facing narrative version. This one is the execution brief — terser,
step-ordered, with explicit STOP conditions.

---

## What Luis gives you (before you start)

| Variable | What it is | Example |
|---|---|---|
| `TAG` | Tag to deploy | `beta-1.10.24` |
| `TARBALL_PATH` | Path on this host to the bare-metal tarball | `/tmp/antigravity-predictor-bare-metal-beta-1.10.24.tar.gz` |
| `TARBALL_SHA256` | Expected sha256 of the tarball | `e30c53f615f46bbdb49e4acb607e7629ffa001c707251083aacb110f87630369` |
| `APP_DIR` | Where the app lives on this host | `/opt/predictor` or `/home/luis/antigravity-predictor` |
| `APP_USER` | System user that runs the services | `predictor` or `luis` |
| `ANTHROPIC_API_KEY` | Optional; enables signal_agent enrichment | value or `""` |
| `AGENT_RELAY_CMD` | Chat backend CLI invocation (see HANDOFF §4) | value or `""` (leaves /api/chat degraded) |
| `SA_INFERENCE_BACKEND` | `enabled` or `disabled` | `disabled` if no API key |
| `ENABLE_BASIC_AUTH` | `true` (default, recommended) or `false` | `true` |
| `OFFSITE_BACKUP_CMD` | Off-site push command; unset = disabled | `""` for now |
| `DEPLOY_DOMAIN` | Optional; if set, run certbot post-install | `""` or `predictor.example.com` |

**If any REQUIRED value is missing** (`TAG`, `TARBALL_PATH`, `TARBALL_SHA256`,
`APP_DIR`, `APP_USER`), STOP and report which. Do not guess.

---

## Phase 0 — Preflight

Bail out with a clear report BEFORE touching anything if the environment
isn't ready. Run each check in order; on first failure, STOP and report.

```bash
# 0.1 — you are actually root (deploy model assumption)
[[ $EUID -eq 0 ]] || { echo "STOP: not root (EUID=$EUID). Hermes must be resident as root."; exit 1; }

# 0.2 — OS is Ubuntu/Debian-family (install.sh uses apt-get)
[[ -f /etc/os-release ]] || { echo "STOP: /etc/os-release missing, cannot verify OS."; exit 1; }
. /etc/os-release
[[ "$ID_LIKE" == *debian* || "$ID" == "ubuntu" || "$ID" == "debian" ]] || \
    { echo "STOP: OS is '$ID' (not Ubuntu/Debian). install.sh uses apt-get."; exit 1; }

# 0.3 — disk: at least 4GB free where APP_DIR will live
target_fs=$(df -P "$(dirname "$APP_DIR")" | tail -1 | awk '{print $4}')
[[ $target_fs -gt 4000000 ]] || { echo "STOP: <4GB free on $(dirname "$APP_DIR") filesystem ($target_fs KB)."; exit 1; }

# 0.4 — network: apt update reaches the archive, PyPI reachable
apt-get update -qq   || { echo "STOP: 'apt-get update' failed. Check network/apt sources."; exit 1; }
curl -sfI --max-time 10 https://pypi.org/simple/ > /dev/null || \
    { echo "STOP: PyPI unreachable. Predictor deps install will fail."; exit 1; }

# 0.5 — required-before-install.sh baseline packages
# install.sh installs python3-venv itself (line 23) but ONLY once it starts.
# If anything on this host was going to try `python3 -m venv` BEFORE
# install.sh runs (e.g. a manual bootstrap attempt), it would fail hard.
# We front-load the baseline packages here so any pre-install.sh probing
# also works, and so install.sh's own apt-get install becomes a no-op.
apt-get install -y -qq curl ca-certificates python3 python3-pip python3-venv git tar || \
    { echo "STOP: baseline apt install failed. See apt output above."; exit 1; }

# 0.6 — port availability (nginx will bind 80/443; predictor stays loopback)
for port in 80 443; do
    if ss -ltn "sport = :$port" | grep -q LISTEN; then
        echo "STOP: port $port already bound by another process:"
        ss -ltnp "sport = :$port"
        exit 1
    fi
done

# 0.7 — verify tarball
[[ -f "$TARBALL_PATH" ]] || { echo "STOP: tarball not found at $TARBALL_PATH."; exit 1; }
echo "$TARBALL_SHA256  $TARBALL_PATH" | sha256sum -c - || \
    { echo "STOP: tarball sha256 mismatch. Do not proceed with a corrupt archive."; exit 1; }
```

If Phase 0 completes without STOP, report `[PREFLIGHT_OK]` and proceed.

---

## Phase 1 — Extract + inspect (no execution yet)

```bash
STAGE="/tmp/deploy-$TAG"
rm -rf "$STAGE" && mkdir -p "$STAGE"
tar -xzf "$TARBALL_PATH" -C "$STAGE"
EXTRACTED="$STAGE/antigravity-predictor-bare-metal-$TAG"
[[ -d "$EXTRACTED" ]] || { echo "STOP: tarball didn't extract as expected."; exit 1; }

# Sanity: install.sh is there and matches the tag (has the beta-1.10.24 flag-parsing
# comment). If this fails, the tarball is from a pre-1.10.24 build and cannot be
# invoked with --app-dir / --user reliably (see HANDOFF §7.19).
grep -q "beta-1.10.24" "$EXTRACTED/deploy/bare-metal/install.sh" || \
    echo "WARN: install.sh doesn't reference 1.10.24 — flag parsing may not work; use APP_DIR/APP_USER env-var form as fallback."
```

Report `[EXTRACTED_OK]` with the path and proceed.

---

## Phase 2 — Deploy (this is where install.sh runs)

```bash
cd "$EXTRACTED"
bash deploy/bare-metal/install.sh --app-dir "$APP_DIR" --user "$APP_USER" \
    2>&1 | tee /tmp/install-$TAG.log
INSTALL_RC=${PIPESTATUS[0]}
[[ $INSTALL_RC -eq 0 ]] || { echo "STOP: install.sh exited $INSTALL_RC. Full log at /tmp/install-$TAG.log."; exit 1; }
```

**Watch for these lines in the install log — capture them for the report:**

- `[INSTALL] Basic auth enabled — user: <U>  password: <P>` — **capture the
  password verbatim**. It is printed once, then only exists as a bcrypt hash
  in `/etc/nginx/.htpasswd`. If missed, Luis will need it regenerated.
- `[INSTALL] Antigravity Predictor installed successfully.` — closing banner.
  Absent = something failed silently in the tail; investigate.

Then populate `.env`:

```bash
ENV_FILE="$APP_DIR/.env"
# Rewrite only the keys Luis gave you; leave the template's other keys alone.
# Use `sed -i` with anchored patterns so a key isn't accidentally matched
# inside a comment.
[[ -n "$ANTHROPIC_API_KEY"      ]] && sed -i "s|^#*ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY|"           "$ENV_FILE"
[[ -n "$AGENT_RELAY_CMD"        ]] && sed -i "s|^#*AGENT_RELAY_CMD=.*|AGENT_RELAY_CMD=$AGENT_RELAY_CMD|"                  "$ENV_FILE"
[[ -n "$SA_INFERENCE_BACKEND"   ]] && sed -i "s|^#*SA_INFERENCE_BACKEND=.*|SA_INFERENCE_BACKEND=$SA_INFERENCE_BACKEND|"   "$ENV_FILE"
[[ -n "$OFFSITE_BACKUP_CMD"     ]] && sed -i "s|^#*OFFSITE_BACKUP_CMD=.*|OFFSITE_BACKUP_CMD=$OFFSITE_BACKUP_CMD|"         "$ENV_FILE"
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Restart services that read .env
systemctl restart predictor agent_relay
[[ "$SA_INFERENCE_BACKEND" == "enabled" ]] && systemctl restart signal_agent
```

Optional HTTPS (only if `DEPLOY_DOMAIN` is set):

```bash
if [[ -n "$DEPLOY_DOMAIN" ]]; then
    certbot --nginx -d "$DEPLOY_DOMAIN" -n --agree-tos -m "$LUIS_EMAIL" || \
        echo "WARN: certbot failed. HTTPS not enabled; deploy is otherwise fine."
fi
```

Report `[DEPLOY_OK]` and proceed.

---

## Phase 3 — Verify (all seven must pass before you report success)

Run each; capture output for the report. On first fail, STOP.

```bash
# 3.1 — predictor.service active
systemctl is-active predictor    | grep -q "^active$" || { echo "FAIL: predictor.service not active"; exit 1; }
systemctl is-active agent_relay  | grep -q "^active$" || { echo "FAIL: agent_relay.service not active"; exit 1; }

# 3.2 — /api/status responds, bound to loopback (not the public interface)
curl -sf --max-time 5 http://127.0.0.1:18910/api/status > /tmp/status-$TAG.json || \
    { echo "FAIL: /api/status not responding on 127.0.0.1:18910"; exit 1; }
ss -ltn "sport = :18910" | grep -q "127.0.0.1:18910" || \
    { echo "FAIL: predictor NOT bound to loopback (public exposure risk)"; ss -ltn "sport = :18910"; exit 1; }

# 3.3 — nginx dashboard behind basic auth (if enabled)
if [[ "$ENABLE_BASIC_AUTH" == "true" ]]; then
    code=$(curl -so /dev/null -w "%{http_code}" http://127.0.0.1/)
    [[ "$code" == "401" ]] || { echo "FAIL: dashboard NOT behind auth (got $code, expected 401)"; exit 1; }
fi

# 3.4 — firewall configured
ufw status | grep -qE "22.*ALLOW|OpenSSH.*ALLOW" || { echo "FAIL: SSH not in ufw allow list"; exit 1; }
ufw status | grep -q "80.*ALLOW"                 || { echo "FAIL: 80 not in ufw allow list"; exit 1; }

# 3.5 — timers scheduled
for t in predictor_backup forge_backup forge_scorecard config_backup sync_offsite macro_refresh; do
    systemctl list-timers "$t.timer" --no-pager | grep -q "$t.timer" || \
        { echo "FAIL: $t.timer not scheduled"; exit 1; }
done

# 3.6 — forge scorecard runs end-to-end (fresh DB → 16 strategies at not_enough_data)
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/forge_scorecard.py" || \
    { echo "FAIL: forge_scorecard.py errored"; exit 1; }
DUMP=$(dirname "$APP_DIR")/$(basename "$APP_DIR")-forge-scorecard/scorecard.txt
[[ -s "$DUMP" ]] || { echo "FAIL: scorecard dump missing/empty at $DUMP"; exit 1; }
grep -q "Not enough data yet (16)" "$DUMP" || echo "WARN: scorecard has trades already or unexpected count — check $DUMP"

# 3.7 — all four backup scripts execute cleanly
for script in backup_signal_log.py backup_forge_db.py backup_config_and_secrets.py sync_backups_offsite.py; do
    sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/$script" || \
        { echo "FAIL: $script errored"; exit 1; }
done
# Confirm backups landed (sync_offsite is expected to log 'skipping' when OFFSITE_BACKUP_CMD is unset)
BACKUP_DIR=$(dirname "$APP_DIR")/$(basename "$APP_DIR")-backups
ls "$BACKUP_DIR" | grep -q "signal_history\." || echo "WARN: no signal_history backup in $BACKUP_DIR"
ls "$BACKUP_DIR" | grep -q "forge\."           || echo "WARN: no forge backup in $BACKUP_DIR"
ls "$BACKUP_DIR" | grep -q "configstate\."     || echo "WARN: no configstate backup in $BACKUP_DIR"
```

Report `[VERIFY_OK]` when all 7 pass.

---

## Phase 4 — Report to Luis

Fixed format so Luis can parse quickly. Return exactly this shape, filled in:

```
[STATUS] deploy-ok | deploy-partial | deploy-fail
[TAG] beta-1.10.24
[HOST] <hostname or IP>
[APP_DIR] <resolved APP_DIR>
[APP_USER] <resolved APP_USER>
[BASIC_AUTH] enabled|disabled  user=<U>  password=<P>       # only if enabled
[HTTPS] enabled(<domain>, expires <YYYY-MM-DD>) | http-only
[TIMERS_NEXT_FIRE]
  predictor_backup.timer:   <ISO ts>
  forge_backup.timer:       <ISO ts>
  forge_scorecard.timer:    <ISO ts>
  config_backup.timer:      <ISO ts>
  sync_offsite.timer:       <ISO ts>
  macro_refresh.timer:      <ISO ts>
[ENV_SET]
  ANTHROPIC_API_KEY:      <set|empty>
  AGENT_RELAY_CMD:        <set|empty>
  SA_INFERENCE_BACKEND:   <value>
  OFFSITE_BACKUP_CMD:     <set|empty>
[VERIFY_SUMMARY]
  3.1 services active:       PASS
  3.2 api loopback-bound:    PASS
  3.3 nginx auth-walled:     PASS
  3.4 firewall configured:   PASS
  3.5 timers scheduled:      PASS
  3.6 scorecard end-to-end:  PASS
  3.7 all four backups ran:  PASS
[WARNINGS]
  <any WARN: lines from Phase 0-3, verbatim; "none" if none>
[LOGS]
  install: /tmp/install-<TAG>.log
  status:  /tmp/status-<TAG>.json
  dump:    <APP_DIR>-forge-scorecard/scorecard.txt
```

Basic-auth password is the ONLY copy — treat it as sensitive, put it in
the report exactly once, do not repeat it in follow-up messages.

---

## STOP conditions — hand back to Luis, do not improvise

The pattern for all of these: report what you saw, DON'T retry, DON'T try
an alternate workaround. Luis decides what to do next.

- `sudo` prompts for a password (means you're not really root — deploy assumption violated).
- Any Phase 0 check fails (OS wrong, disk full, PyPI unreachable, port already in use, tarball corrupt).
- `install.sh` exits non-zero (partial install is worse than none — don't retry blindly, the script is designed to fail fast).
- Any Phase 3 verification fails (partial deploy is not "deployed").
- The basic-auth password prints but you didn't capture it — abort and tell Luis, so he can decide whether to reinstall or rotate the password by hand.
- **The venv creation fails** (`install.sh` says `[ERROR] venv creation failed`) — this is HANDOFF §7.7's recurring silent failure. Do NOT retry blindly. Capture the previous 30 lines of `/tmp/install-$TAG.log`, report them, wait.

---

## Common failure recovery (only after Luis says to retry)

- **Venv silent fail:** manually `python3 -m venv $APP_DIR/.venv && $APP_DIR/.venv/bin/pip install -r $APP_DIR/requirements.txt && chown -R $APP_USER:$APP_USER $APP_DIR/.venv`, then rerun `install.sh` — it's idempotent for everything else.
- **Port already bound:** the previous process needs to die first. `systemctl stop predictor agent_relay signal_agent forge` and any prior deploy's services, then retry. If it's a non-predictor process holding the port, escalate to Luis — don't kill things Hermes didn't start.
- **`apt-get update` fails on a stale mirror:** try `apt-get update --allow-releaseinfo-change` once; if still failing, escalate.
- **`/api/chat` returns 503:** expected if `AGENT_RELAY_CMD` isn't set to something that actually works on this host. Verify by hand with `sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/tools/agent_chat_relay.py` and `curl http://127.0.0.1:8645/health` before touching `agent_relay.service`. See HANDOFF §7.5 and `deploy/bare-metal/LIVE_DEPLOY_NOTES.md` #7.

---

## Deep-context pointers (read only if you need to understand why)

Inside `$EXTRACTED`:

- `HANDOFF.md` — running narrative. §2 for tag currency, §7.6-§7.19 for the recent chain, §5 for release conventions.
- `docs/DATA_INVENTORY.md` — every piece of durable data, backup coverage, restore steps.
- `docs/RESTORE_PLAYBOOK.md` — runbook for the "carry data forward from a prior host" scenario.
- `deploy/bare-metal/LIVE_DEPLOY_NOTES.md` — 7 numbered gotchas from prior live deploys. Read #7 before touching agent_relay.

Do not read these speculatively. They exist for when something breaks.
