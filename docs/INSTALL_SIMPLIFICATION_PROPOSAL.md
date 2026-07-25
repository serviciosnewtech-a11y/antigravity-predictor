# install.sh simplification proposal

Subtractive audit of `deploy/bare-metal/install.sh` (543 lines as of
beta-1.10.29) plus its bare-metal sibling scripts. Read-only proposal — no
changes applied.

Sibling scripts inspected (for duplication surface only): `deploy.sh`,
`hermes_deploy.sh`, `verify.sh`, `rollback.sh`, `hermes_deploy.conf.example`.

---

## 1. What the install currently does

1. Parse `--app-dir` / `--user` flags (both `--flag value` and `--flag=value` forms) and `-h/--help`.
2. Print a 9-line "how to re-run as root" message if not EUID 0 and exit.
3. `apt-get update` + install python3, pip, venv, nginx, certbot, git, apache2-utils, acl.
4. Create the `$APP_USER` system user if missing.
5. `mkdir -p` the app dir tree.
6. `rsync` src/, models/, deploy/bare-metal/, tools/, dashboard/ into `$APP_DIR`.
7. Copy retrain_all.sh, requirements.txt, config.json.
8. `sed` config.json `host: 0.0.0.0` → `127.0.0.1`.
9. `chown -R` and `chmod +x retrain_all.sh`.
10. Create venv, verify `.venv/bin/python` exists, pip install requirements.txt (or fallback list).
11. Write ~100-line `.env` template if it doesn't exist.
12. `sed` path substitutions into predictor/macro_refresh/signal_agent/agent_relay service files → `/etc/systemd/system/`.
13. Create `$BACKUP_DIR` outside app dir.
14. Same `sed` + install for predictor_backup, forge_backup, config_backup, sync_offsite units.
15. Create `$FORGE_SCORECARD_DIR` outside app dir.
16. Same `sed` + install for forge_scorecard unit.
17. `systemctl daemon-reload` + enable 9 units.
18. Generate htpasswd (unless `ENABLE_BASIC_AUTH=false`), print credentials in two formats.
19. Copy nginx.conf → sites-available, strip auth_basic lines if disabled, symlink → sites-enabled, remove default, `nginx -t`, reload.
20. If ufw exists: allow 22/80/443, `ufw enable`.
21. Run initial `fetch_macro.py` (best-effort, warn on failure).
22. `systemctl start` predictor + 6 timers + agent_relay.
23. Grep .env for `SA_INFERENCE_BACKEND`; if enabled, start signal_agent.
24. If `$SUDO_USER` set and ≠ app user: `setfacl` recursive read+traverse on app dir, backup dir, scorecard dir.
25. Print a ~65-line status banner (URLs, log commands, backup instructions, off-site sync status, forge scorecard paths).

---

## 2. Complexity that can be cut

| Section (line range) | What it does | Why it's cuttable | Risk if removed |
|---|---|---|---|
| 22-25 | 4-line comment narrating the beta-1.10.24 flag-parsing bugfix | Historical; the code below is self-explanatory | None |
| 30-31 | `--flag=value` variants alongside `--flag value` | Two forms for one thing; no evidence anyone uses the `=` form | Trivial; caller would use space form |
| 46-59 | 4-line preamble comment + 9-line multi-invocation root-error message | `bash` refusing on permission-denied fails just as loudly with one line: `[[ $EUID -eq 0 ]] \|\| { echo "run as root"; exit 1; }` | Slightly terser error, but sudo is universally understood |
| 82-92 | Comments narrating why tools/ and dashboard/ get copied | Historical debug narrative; the rsync line itself is 3 lines | None — rsync is self-evident |
| 96-99, 101-112 | 17 lines of comments justifying the config.json copy and the 0.0.0.0→127.0.0.1 sed | The single `sed` line (113) is load-bearing; the essay above it is not | None if reduced to a one-line comment |
| 121-128 | 8-line comment justifying the venv `-x` check | `python3 -m venv` under `set -e` already aborts on failure; the extra `[[ -x ]]` check adds one message but nothing systemd wouldn't catch on next start | Low — swaps an install-time abort for a service-start abort at same failure mode |
| 141-244 | ~100-line `.env` template with three separate multi-paragraph comment blocks (Pattern A/B, signal-agent, off-site sync) | The tarball ships `.env.example` and HANDOFF §7.11+/LIVE_DEPLOY_NOTES.md already document these traps in narrative form. A 20-line template with `KEY=` and one-line comments would leave the same seams | Low — operators still need to edit .env; a shorter template does not remove any knob |
| 265-273 | 9-line comment explaining agent_relay is always installed | The `sed`+redirect line does the work; comment is duplicated in the .env template | None |
| 277-283, 292-295, 300-306, 311-317, 322-327 | ~30 lines of per-unit narrative comments before each `sed` block | Each comment restates what HANDOFF §7.11-7.17 already covers | None |
| 340-358 | 19-line comment block above basic-auth setup | Repeats what nginx.conf and README already document | None |
| 389-395 | 7-line firewall comment | Self-evident from the `ufw allow` lines below it | None |
| 396-409 | `command -v ufw &>/dev/null` guard + fallback WARN | ufw is a standard Ubuntu package. Add `ufw` to the apt line (66) and delete the whole guard | None — apt install ufw succeeds on every Ubuntu supported host |
| 411-415 | Initial macro fetch (sudo -u app-user python3 fetch_macro.py) | `macro_refresh.timer` fires within an hour anyway; the "warm-up" data is not required for service start | Low — first hour of dashboard has empty macro chart until the timer runs |
| 426-427, 430-435 | 8 lines of `agent_relay always started` + `SA_INFERENCE_BACKEND` explanation comments | Duplicates HANDOFF and the .env template itself | None |
| 436-443 | Grep the freshly-written .env for `SA_INFERENCE_BACKEND` to decide whether to `systemctl start signal_agent` | The .env template defaults it to `disabled`; a fresh install ALWAYS lands in the "don't start" branch. Just leave signal_agent enabled-but-not-started; the operator starts it when they configure a backend | None — operator already has to edit .env; adding one `systemctl start` is not friction worth 8 lines to auto-detect |
| 446-476 | 12-line comment + 21-line INSPECTOR_USER ACL block (beta-1.10.29) | Same effect as `chmod 755 $APP_DIR` at install time, or two lines of `setfacl` at the bottom without the SUDO_USER detection dance. Real question: is the inspector-ACL machinery earning its 33 lines, or would a simpler default do? | Low if replaced with an unconditional `setfacl` line for a known inspector; medium if removed entirely (returns §7.7 friction) — see Open Question #1 |
| 478-542 | 65-line status banner (URLs, log commands, retrain, backup how-tos, off-site sync status probe, forge scorecard runbook) | Everything in the banner is in the README/RESTORE_PLAYBOOK/HANDOFF. First-time operators read those, not scrollback. Keep the BASIC_AUTH credential print (that IS printed once) and 3-4 pointers, drop the rest | Low — nothing operational depends on the banner; it's documentation-in-shell |
| 530-534 | grep .env for OFFSITE_BACKUP_CMD to print one of two banner lines | Cosmetic; goes with the banner cut above | None |

---

## 3. Recommended minimum viable install

The ideal `install.sh` on a fresh Ubuntu 22.04 VPS: (1) apt install every
package the runtime and firewall need, (2) create the app user, (3) rsync
the app into `$APP_DIR` and force config.json to loopback binding, (4)
create the venv and pip install requirements, (5) write a short `.env`
template if none exists, (6) sed-substitute paths into all systemd units
and enable them in one `systemctl enable` line, (7) generate htpasswd and
drop nginx site config, (8) allow 22/80/443 in ufw, (9) start
predictor+agent_relay+timers, (10) print the BASIC_AUTH line and three
pointers (`journalctl -u predictor`, `$APP_DIR/.env`, README section). No
initial macro fetch (the timer runs shortly), no SA_INFERENCE_BACKEND
grep-and-branch (default disabled = don't start), no essay-length inline
comments (HANDOFF is the audit trail), no ACL machinery unless Luis
confirms he wants an inspector user by default. Target: ~180-220 lines,
down from 543.

---

## 4. Concrete diff proposal

Groups ordered top-to-bottom through install.sh. Line numbers are current tip.

```
# CUT: deploy/bare-metal/install.sh lines 22-25
# Reason: historical narrative about the flag-parsing bugfix; code below is self-explanatory
- # CLI flag parsing (bugfix beta-1.10.24 — prior versions silently ignored
- # flags shown in the usage comment, an active trap for anyone following the
- # advertised interface). Any unrecognised arg is a hard error rather than
- # silently accepted, matching bash's own convention for --unknown flags.
```

```
# CUT: deploy/bare-metal/install.sh lines 30-31
# Reason: --flag=value duplicates --flag value; no evidence anyone uses the = form
-         --app-dir=*) APP_DIR="${1#*=}";  shift ;;
-         --user=*)    APP_USER="${1#*=}"; shift ;;
```

```
# CUT: deploy/bare-metal/install.sh lines 46-49
# Reason: comment restates what the code below already says
- # Actionable error: the install genuinely needs root (apt-get, /etc/systemd,
- # /etc/nginx, ufw). There is no non-sudo fallback -- attempting one would
- # fail deeper into the script at the first apt call. Point at the two
- # working invocations so the operator doesn't retry the same wrong thing.
```

```
# CUT: deploy/bare-metal/install.sh lines 51-60
# Reason: 9-line multi-invocation guidance replaceable with one echo
- if [[ $EUID -ne 0 ]]; then
-     echo "[ERROR] install.sh must run as root. This is not optional -- the script"    >&2
-     echo "        installs apt packages, writes systemd units and nginx config,"      >&2
-     echo "        and configures ufw. Re-run with one of:"                            >&2
-     echo ""                                                                            >&2
-     echo "          sudo bash $0 --app-dir <path> --user <name>"                      >&2
-     echo "          APP_DIR=<path> APP_USER=<name> sudo -E bash $0"                   >&2
-     echo ""                                                                            >&2
-     echo "        The -E on the second form preserves your env vars through sudo;"    >&2
-     echo "        without it, APP_DIR/APP_USER get reset to defaults inside sudo."    >&2
-     exit 1
- fi
# NOTE: replace with a one-liner as described in §3 (not shown here — additive)
```

```
# CUT: deploy/bare-metal/install.sh lines 82-91
# Reason: audit-trail narration for the rsync lines
- # tools/ — needed for agent_chat_relay.py (the local, no-API-key CLI-agent
- # chat backend; see agent_relay.service below). Previously not copied at
- # all since nothing in the systemd product used anything from tools/ yet.
- ...
- # dashboard/ (index.html/app.js/style.css) — predictor_server.py mounts this
- # via FastAPI StaticFiles at "/". Without this copy the mount silently no-ops
- # (guarded by an os.path.exists check) and nginx's "location /" proxy gets a
- # 404 from FastAPI for every request — the dashboard would never load on a
- # fresh install. Found during pre-redeploy verification, not a live incident.
```

```
# CUT: deploy/bare-metal/install.sh lines 95-98 and 101-112
# Reason: 16 lines of comment for one cp + one sed
- # config.json — predictor_server.py hard-requires this (raises and refuses to
- # start if missing from both src/config.json and $APP_DIR/config.json). This
- # copy step was missing entirely, which would have crash-looped predictor.service
- # on every fresh install. Found during pre-redeploy verification.
- ...
- # Force server.host to loopback for THIS product specifically. The repo's
- # config.json ships "0.0.0.0" because that's correct and necessary for
- # ... (12 lines of essay) ...
- # Found 2026-07-23 auditing what "safely expose the dashboard" requires.
```

```
# CUT: deploy/bare-metal/install.sh lines 121-128
# Reason: `set -e` + venv step already aborts loudly; the -x guard is belt-on-belt
- # Verify venv actually created before continuing -- addresses the §7.7
- # recurring failure mode where a partial/silent venv creation left
- # predictor.service crash-looping on 203/EXEC. This makes it fail here,
- # with an actionable message, instead of much later at service start
- # with a cryptic exit code. Root cause of the underlying failure is
- # still unknown -- if this triggers, capture the previous few lines of
- # install.sh output to help diagnose (disk full? python3-venv package
- # broken? permission on APP_DIR? -- all real hypotheses, none confirmed).
- [[ -x "$APP_DIR/.venv/bin/python" ]] || die "venv creation failed: $APP_DIR/.venv/bin/python is missing or not executable. Check disk space, that python3-venv is installed (apt install python3-venv), and that $APP_DIR is writable by root."
# NOTE: If Luis wants to keep the check for §7.7 protection, keep line 129 alone and drop 121-128 (comment only).
```

```
# CUT: deploy/bare-metal/install.sh lines 146-196, 202-244
# Reason: ~100-line self-documenting .env template with three multi-paragraph essay blocks
# The heredoc is the biggest single cost in the file. .env.example ships in the tarball,
# HANDOFF §7.11+ + LIVE_DEPLOY_NOTES.md document these traps. Replace with a ~20-line
# skeleton (KEY= plus one-line comments). Exact replacement text belongs in §3, not here.
- (lines 146-244 heredoc contents, minus the writer lines 145 and 245-247)
```

```
# CUT: deploy/bare-metal/install.sh lines 265-271
# Reason: HANDOFF §7.11 already documents this; the sed line below is self-evident
- # agent_relay.service — local, no-API-key CLI-agent chat backend
- # (tools/agent_chat_relay.py). Always installed/enabled: it degrades
- # gracefully (health-check reports agent_binary_exists=false, /api/chat
- # calls return a real 502 instead of a crash) if AGENT_RELAY_CMD's binary
- # isn't actually on this host, so there's no harm in it always running —
- # but it does mean it's only USEFUL once .env's AGENT_RELAY_CMD points at
- # a real installed agent. See the .env template below.
```

```
# CUT: deploy/bare-metal/install.sh lines 277-283
# Reason: HANDOFF §7.16 covers this
- # predictor_backup — periodic durable backup of signal_history.db (...)
- # ... (7 lines) ...
```

```
# CUT: deploy/bare-metal/install.sh lines 292-295
# Reason: HANDOFF §7.11 covers this
- # forge_backup -- periodic durable backup of forge.db (...)
- # ... (4 lines) ...
```

```
# CUT: deploy/bare-metal/install.sh lines 300-306
# Reason: HANDOFF §7.16 covers this
- # config_backup -- periodic durable backup of the "unprotected" sources
- # ... (7 lines) ...
```

```
# CUT: deploy/bare-metal/install.sh lines 311-317
# Reason: HANDOFF §7.17 covers this
- # sync_offsite -- push /opt/predictor-backups to an operator-configured
- # ... (7 lines) ...
```

```
# CUT: deploy/bare-metal/install.sh lines 322-327
# Reason: HANDOFF §7.10 covers this
- # forge_scorecard -- periodic evaluation pass over Forge trade history.
- # ... (6 lines) ...
```

```
# CUT: deploy/bare-metal/install.sh lines 340-358
# Reason: 19-line essay above 15 lines of code; README and nginx.conf already cover the intent
- # Every route in predictor_server.py is unauthenticated by design (...)
- # ... (19 lines) ...
```

```
# CUT: deploy/bare-metal/install.sh lines 389-395
# Reason: self-evident from the ufw commands below
- # Defense in depth on top of the config.json loopback-bind fix above: even
- # ... (7 lines) ...
```

```
# CUT: deploy/bare-metal/install.sh lines 396, 403-409
# Reason: add `ufw` to the apt line (66) and delete the command-v guard + WARN branch entirely
- if command -v ufw &>/dev/null; then
-     ...
- else
-     log "WARN: ufw not found — skipping firewall setup. Ports 18910/8645 are" \
-         "only bound to 127.0.0.1 (see config.json fix above), but with no" \
-         "host firewall at all, confirm your cloud provider's own security" \
-         "group/network ACL restricts inbound traffic before exposing this" \
-         "host publicly."
- fi
# NOTE: keep lines 397-402 (the actual ufw commands) inside a plain sequence, no guard.
```

```
# CUT: deploy/bare-metal/install.sh lines 411-415
# Reason: macro_refresh.timer fires within an hour anyway; not required for service start
- # ── Initial macro fetch ───────────────────────────────────────────────────────
- log "Running initial macro data fetch…"
- sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/src/fetch_macro.py" \
-     --data-dir "$APP_DIR/data/macro" --days 730 || \
-     log "WARN: initial macro fetch failed — run manually before retraining."
```

```
# CUT: deploy/bare-metal/install.sh lines 426-427, 430-443
# Reason: the .env template defaults SA_INFERENCE_BACKEND=disabled — the grep always
#         lands in the "don't start" branch on a fresh install. Leave signal_agent
#         enabled-but-not-started; the operator starts it after editing .env.
- # Always started — degrades gracefully (see agent_relay.service comment
- # above) rather than crashing if AGENT_RELAY_CMD's binary isn't installed.
- ...
- # signal_agent's on/off switch is SA_INFERENCE_BACKEND, not a specific key —
- # ... (6 lines) ...
- SA_BACKEND_VALUE="$(grep -E '^SA_INFERENCE_BACKEND=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
- if [[ -n "$SA_BACKEND_VALUE" && "$SA_BACKEND_VALUE" != "disabled" ]]; then
-     systemctl start signal_agent
-     log "signal_agent.service started."
- else
-     log "WARN: SA_INFERENCE_BACKEND is disabled (or unset) in $ENV_FILE — signal_agent NOT started."
-     log "      Edit $ENV_FILE (SA_INFERENCE_BACKEND=enabled), then: systemctl start signal_agent"
- fi
```

```
# CUT: deploy/bare-metal/install.sh lines 446-476
# Reason: inspector-user ACL (beta-1.10.29) is 33 lines of code+comment.
# Either drop entirely (return to sudo-for-diagnostics) or replace with 2 lines
# of unconditional setfacl. See Open Question #1.
- # ── Inspector-user ACL grant ─────────────────────────────────────────────────
- # 0700 on $APP_DIR (predictor's private home dir) blocks anyone but predictor
- # ... (12 lines of comment) ...
- INSPECTOR_USER="${INSPECTOR_USER:-${SUDO_USER:-}}"
- if [[ -n "$INSPECTOR_USER" && "$INSPECTOR_USER" != "$APP_USER" && "$INSPECTOR_USER" != "root" ]]; then
-     if id "$INSPECTOR_USER" &>/dev/null; then
-         log "Granting read+traverse ACL to inspector user: $INSPECTOR_USER"
-         setfacl -Rm  "u:$INSPECTOR_USER:rX" "$APP_DIR"
-         setfacl -dRm "u:$INSPECTOR_USER:rX" "$APP_DIR"
-         for extra in "$BACKUP_DIR" "$FORGE_SCORECARD_DIR"; do
-             [[ -d "$extra" ]] || continue
-             setfacl -Rm  "u:$INSPECTOR_USER:rX" "$extra"
-             setfacl -dRm "u:$INSPECTOR_USER:rX" "$extra"
-         done
-         log "  (mutation still requires sudo — this grant is read-only)"
-     else
-         log "WARN: INSPECTOR_USER='$INSPECTOR_USER' does not exist; skipping ACL grant"
-     fi
- else
-     log "No INSPECTOR_USER to grant read ACL (SUDO_USER=${SUDO_USER:-<unset>}, APP_USER=$APP_USER)"
-     log "  To add later:  sudo setfacl -Rm u:<user>:rX $APP_DIR"
- fi
```

```
# CUT: deploy/bare-metal/install.sh lines 478-542
# Reason: 65-line status banner duplicates README + RESTORE_PLAYBOOK + HANDOFF.
# Keep only: (1) the BASIC_AUTH creds line (already printed at 368-370, so this is redundant),
# (2) 3-4 pointers (edit .env, journalctl, retrain path). Everything else goes.
- log "======================================================"
- log " Antigravity Predictor installed successfully."
- log ""
- log " API:       http://<vps-ip>/api/status"
- ...
- (65 lines total)
- log "======================================================"
# NOTE: the OFFSITE_STATUS_LINE grep at 530-534 is part of this cut.
```

**Approximate line removal count:** ~330 lines out of 543 (net script drops
to ~213 lines once the replacement one-liners for §3's shortened root
error and .env template are added — which are additive and thus not shown
here per the ground rules).

---

## 5. Open questions for Luis

1. **Inspector-user ACL (beta-1.10.29, lines 446-476, 33 lines):** cut
   entirely and return to `sudo cat` / `sudo ls` for diagnostics, OR
   replace the SUDO_USER-detection dance with two unconditional
   `setfacl` lines targeting a hardcoded inspector user (e.g.
   `hermes`)? The current logic is defensible but expensive.

2. **`.env` template heredoc (lines 146-244, ~100 lines):** OK to strip
   the three-paragraph essays and replace with a 20-line skeleton whose
   comments each fit on one line? Operators would rely on
   `.env.example` (already shipped) and HANDOFF §7.11+ for the deep
   context.

3. **Initial macro fetch (lines 411-415):** OK to drop? The
   `macro_refresh.timer` runs within an hour of install. First-hour
   dashboard would show an empty macro chart until the timer fires.

4. **SA_INFERENCE_BACKEND grep-and-branch (lines 436-443):** OK to
   drop the auto-start logic since a fresh `.env` ALWAYS lands in the
   "don't start" branch? Operator would run `systemctl start
   signal_agent` themselves after editing .env — one extra command
   they're editing .env for anyway.

5. **Post-install banner (lines 478-542, 65 lines):** OK to compress
   to ~8 lines (BASIC_AUTH reminder + edit-.env pointer + journalctl
   pointer + link to README)? The current banner is essentially
   inline documentation.
