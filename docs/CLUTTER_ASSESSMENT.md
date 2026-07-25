# Clutter Assessment -- Antigravity Predictor

Audit against `beta-1.10.28` (HEAD `f542dc8`), 2026-07-25. Walks the repo
tree, flags files that are stale, orphaned, or duplicated, and proposes
**delete / archive / keep** for each with a one-line justification.

Nothing here has been executed. Every proposed action is a copy-pasteable
command at the bottom for Luis to run in his own shell (the Cowork mount is
`unlink(2)`-restricted -- see HANDOFF §7.15 for why `git mv` / `git rm`
work from a real shell but not from this session).

Verdict legend: **DELETE** = remove outright, no preserved copy.
**ARCHIVE** = move to `docs/archive/` or `models/archive/` for provenance.
**KEEP** = leave in place, listed only so the audit is complete.

---

## 1. Root-level session-leftover files

Confirmed clean. The four items called out in HANDOFF §7.15
(`FINISH_BETA_1_10_15.sh`, `FINISH_BETA_1_10_15_MSG.txt`,
`outputs_test.txt`, plus the older doc pile) are already `git rm --cached`d
in the tracked tree; the physical-delete list at the bottom of §7.15 covers
Luis's remaining working-tree cleanup and doesn't need repeating here.

**No new `FINISH_*` / `*_MSG.txt` / `outputs_*.txt` files found at root.**
Root today contains only the intended files: `HANDOFF.md`, `README.md`,
`config.json`, `requirements.txt`, `retrain_all.sh`, `run_local.sh`,
`run_monolith.sh`, `.env`, `.env.example`, `.dockerignore`, `.gitignore`.

---

## 2. Stale docs and duplicated content

| File | Verdict | Justification |
|---|---|---|
| `docs/TUTOR_AGENT_INSTALL_DOSSIER.json` (222 lines) | **DELETE** (or ARCHIVE if provenance matters) | Machine-readable install dossier from 2026-07-19 pinning commit `e0a83e9`. Nothing references it in the current repo (grep is clean). The current deploy story is `docs/HERMES_DEPLOY_PROMPT.md` + `docs/DEPLOY_NONINTERACTIVE.md` + `deploy/bare-metal/{hermes_deploy,deploy,verify,rollback}.sh` -- this JSON pre-dates all of them. |
| `docs/reporting/LOGGING_SPEC.md` (109 lines) | **KEEP** or **ARCHIVE** -- Luis's call | Proposal doc from beta-1 era for parallel-strategy logging. `forge/` shipped instead (beta-1.10.15). Not obviously stale (Forge doesn't claim to be a full replacement for this spec) but not driving any current work either. |
| `docs/reporting/REPORT_TEMPLATE.md` (66 lines) | **KEEP** or **ARCHIVE** -- Luis's call | Paired with LOGGING_SPEC.md above; same status. |
| `docs/archive/*` (9 files, ~114 KB) | **KEEP** | Already the archive location per §7.15. |
| `docs/REMOTE_DEPLOY_HANDOFF.md` (355 lines) | **KEEP** | Cross-referenced by the current install.sh CLI-flag fix (HANDOFF §7.19). Human-readable narrative counterpart to `deploy.sh`/`hermes_deploy.sh`. |
| `docs/HERMES_DEPLOY_PROMPT.md` (292 lines) | **KEEP** | Human-facing narrative for the Hermes deploy path (§7.21). |
| `docs/DEPLOY_NONINTERACTIVE.md` (120 lines) | **KEEP** | Documents the NOPASSWD sudoers seam for `deploy.sh` (§7.22). |
| `docs/RESTORE_PLAYBOOK.md` (268 lines) | **KEEP** | Ordered runbook for `tools/restore_from_backup.sh` (§7.18). |
| `docs/ISSUE_TEMPLATE/deploy-blocker.md` | **KEEP** | Structured template used by `deploy.sh` failure reports (§7.22). |

### 2.1 Stale references inside `README.md`

`README.md` still points at three files that no longer exist at the paths
it names (all removed or moved in the §7.15 housekeeping):

- Line 47: `TARGET_HERMES_DEPLOY_PROMPT.md` (removed from tracking).
- Line 54: `ANTIGRAVITY_PREDICTOR_BETA1_FULL_TECHNICAL_DOSSIER.md` (moved
  to `docs/archive/`).
- Line 57: `docs/plans/BETA_1_1_LOGGING_IMPLEMENTATION_PLAN.md` (removed
  from tracking).

**Verdict: EDIT** -- README.md needs a small fix-up. Not clutter, but the
broken links compound the impression that "everything is scattered."
Rewrite the "Hermes target-agent deployment" and "Technical dossier"
sections to point at the current `docs/HERMES_DEPLOY_PROMPT.md` +
`docs/DEPLOY_NONINTERACTIVE.md` + `docs/reporting/*.md` (or drop those
sections entirely if superseded).

---

## 3. Orphaned scripts (nothing calls them)

Cross-checked every script in `tools/` against systemd units in
`deploy/bare-metal/*.service`, `run_monolith.sh`, `run_local.sh`,
`retrain_all.sh`, `deploy/docker/Makefile`, and repo-wide grep.

| Script | Called by | Verdict |
|---|---|---|
| `tools/agent_chat_relay.py` | `agent_relay.service`, `run_monolith.sh` | KEEP |
| `tools/backup_signal_log.py` | `predictor_backup.service` | KEEP |
| `tools/backup_forge_db.py` | `forge_backup.service` | KEEP |
| `tools/backup_config_and_secrets.py` | `config_backup.service` | KEEP |
| `tools/sync_backups_offsite.py` | `sync_offsite.service` | KEEP |
| `tools/forge_scorecard.py` | `forge_scorecard.service` | KEEP |
| `tools/restore_from_backup.sh` | `docs/RESTORE_PLAYBOOK.md`; manual | KEEP |
| `tools/package_release.sh` | Manual (per HANDOFF §5) | KEEP |
| `tools/recalibrate_thresholds.py` | Manual (referenced in HANDOFF §7) | KEEP |
| `tools/retrain_live_features.py` | Manual (referenced in HANDOFF §7) | KEEP |
| `tools/run_tests.sh` | Manual dev wrapper | KEEP |
| `tools/soak_test.py` | Manual (documented in HANDOFF as QA tool) | KEEP |
| `tools/admin_chat.py` | `run_monolith.sh` docstring; manual pairing with `admin_agent/server.py` | KEEP |
| `tools/clean_persona_memory.py` | Occasional operator tool (referenced in `test_chat_unification.py`) | KEEP |
| `tools/diagnose_gold.sh` | **Nothing.** One-shot diagnostic from beta-1.1 era ("this whole comment refers to a bare-metal-monolith deploy of the antigravity-predictor-beta1.1 package"). | **ARCHIVE** -- move to `docs/archive/tools/` or delete. Kept a copy is cheap. |

---

## 4. Ephemeral runtime files sitting in the repo working tree

These are `.gitignore`'d and therefore not clutter *in git*, but they clog
`ls` and confuse "what does this repo actually contain?" scans.

| Path | Status | Verdict |
|---|---|---|
| `.venv/` (683 MB) | Gitignored | KEEP -- required to run |
| `.retrain_cache/` (100 MB) | Gitignored | KEEP -- required for fast retrain |
| `.pytest_cache/` | Gitignored | KEEP -- `pytest` recreates |
| `logs/` (all contents) | Gitignored | KEEP dir, see 4.1-4.3 for individual files |
| `forge_data/forge.db` (80 KB) | Gitignored | KEEP -- live-writable DB |

### 4.1 `logs/tutor_memory.jsonl` (97 bytes, 1 line)

**DELETE.** The `/api/tutor-chat` endpoint was merged into `/api/chat` on
2026-07-23 (see `src/predictor_server.py:1349` comment,
`test_chat_unification.py`). Nothing writes to this file now. Only content
is `{"user": "test user msg", "agent": "test tutor reply"}` from 2026-07-20.

### 4.2 `logs/work_progress/H13_RETRAIN_LIVE_SIGNALS_20260719T0835Z.md`

**ARCHIVE** (or DELETE). One-off retrain-session report from 2026-07-19.
No code path references it. If historical provenance matters, move to
`docs/archive/`; otherwise delete. Gitignored either way (`logs/` is in
`.gitignore`) so this is a working-tree-only action.

### 4.3 `logs/admin_agent_audit.log`, `logs/crypto_operator_memory.jsonl`, `logs/signal_history.db`

**KEEP.** Live runtime state -- see `DATA_INVENTORY.md` rows 1, 3, 12.

---

## 5. Model archive

| Path | Verdict | Justification |
|---|---|---|
| `models/archive/backup_pre_expand_20260719_164210/` (644 KB, tracked) | **DELETE** (open question below) | HANDOFF §7.15 explicitly calls these "ephemeral" and notes "they served their purpose during the July 19 H-13 remediation and no live path references them." They're only kept as archive-not-delete out of caution. Grep is clean. |
| `models/archive/backup_pre_h13_retrain_20260719_083506/` (4.5 MB, tracked) | **DELETE** (open question below) | Same as above. |
| `models/archive/backup_pre_htf_history_expand_20260719_170000/` (560 KB, tracked) | **DELETE** (open question below) | Same as above. |

**Open question for Luis:** the three `models/archive/backup_pre_*/` dirs
are tracked in git (5.7 MB in the repo). HANDOFF §7.15 preserved them as
"archive" instead of deleting outright, but DATA_INVENTORY row 13/16
explicitly calls them ephemeral. If nothing in the H-13 story needs them
any more, `git rm -r` recovers the space and simplifies the tree.

---

## 6. Empty and near-empty directories

| Path | Verdict | Justification |
|---|---|---|
| `.git/branches/` | KEEP -- git internal | Standard empty subdir; git creates on init. |
| `.git/objects/info/` | KEEP -- git internal | Same. |
| `.venv/include/` | KEEP -- venv internal | Standard empty subdir. |

**No project-owned empty directories found.**

---

## 7. Git-plumbing detritus (ops cleanup, NOT for `git rm`)

Not repo clutter; live-shell cleanup for Luis. Listed separately so it
isn't mistaken for `git rm` candidates.

- **`.git/foo`, `.git/xxx`** -- two zero-byte files inside `.git/`,
  dated 2026-07-24. Likely `touch`-and-forget from the FUSE-mount
  workaround work. Safe to delete from a real shell.
- **`.git/objects/*/tmp_obj_*`** -- 121 stale temp objects from
  interrupted `git write-tree` / `git commit-tree` calls (the FUSE
  `unlink(2)` restriction leaves these behind on every failed step; see
  HANDOFF §7.15 for the direct-write pattern that leaks them). Safe to
  delete from a real shell; git regenerates as needed. `git gc --prune=now`
  (from Luis's shell, not this session) is the cleanest sweep.
- **`.git/index.lock`** -- observed on this audit (`git status --ignored`
  warns "unable to unlink"). Same FUSE root cause. `rm -f
  .git/index.lock` from Luis's shell.
- **`.git/refs/tags/beta-1.10.15-pre-forge-scorecard`,
  `.git/refs/tags/beta-1.10.16-pre-cleanup`,
  `.git/refs/tags/beta-1.10.20-pre-cleanup`** -- three "pre-something"
  scaffolding tags from the direct-write ceremony. Safe to delete
  (`git tag -d <name>`) if Luis doesn't want them cluttering `git tag -l`.
  Not tracked in `../releases/` per §5, so no external artifact depends
  on them.

---

## 8. Beta-1.10.x branch/patch files

**None found.** No `beta-1.10.*.patch`, `beta-1.10.*.diff`, or
`beta-1.10.*.tar.gz` at repo root or in `deploy/`. Release tarballs live
outside the repo (`../releases/antigravity-predictor/`) per HANDOFF §5,
which is the intended pattern.

Note: `beta-1.11` tag is a known trap (see HANDOFF §2). Not a file
clutter issue -- just a numbering-history footgun -- so no action here.

---

## 9. Prioritized cleanup list -- top 10 in "do this first" order

Order is: what has the highest signal-per-second, cheapest to reverse if
wrong. Every command below assumes `cd /media/hermes/Storage/git/antigravity-predictor`
and is run from Luis's own shell (not the Cowork mount).

### 1. Fix the three broken links in README.md

Edit `README.md` lines 42-57. Remove the three dead references
(`TARGET_HERMES_DEPLOY_PROMPT.md`,
`ANTIGRAVITY_PREDICTOR_BETA1_FULL_TECHNICAL_DOSSIER.md`,
`docs/plans/BETA_1_1_LOGGING_IMPLEMENTATION_PLAN.md`); either delete the
sections or repoint them at `docs/HERMES_DEPLOY_PROMPT.md`,
`docs/DEPLOY_NONINTERACTIVE.md`, and `docs/reporting/*.md`.

### 2. Delete the obviously-orphan working-tree files

```bash
rm -f logs/tutor_memory.jsonl
rm -f .git/foo .git/xxx
rm -f .git/index.lock
```

### 3. Delete the stale install dossier

```bash
git rm docs/TUTOR_AGENT_INSTALL_DOSSIER.json
```

(If provenance matters, `git mv docs/TUTOR_AGENT_INSTALL_DOSSIER.json
docs/archive/` instead. The JSON is machine-readable and any future
consumer can just check it out of the git history.)

### 4. Archive `tools/diagnose_gold.sh`

Written for the beta-1.1-era bare-metal-monolith package; predates the
current systemd product entirely.

```bash
mkdir -p docs/archive/tools
git mv tools/diagnose_gold.sh docs/archive/tools/diagnose_gold.sh
```

### 5. Archive or delete `logs/work_progress/`

Old H-13 retrain session note; nothing references it.

```bash
# archive:
mkdir -p docs/archive/sessions
mv logs/work_progress/H13_RETRAIN_LIVE_SIGNALS_20260719T0835Z.md \
   docs/archive/sessions/
rmdir logs/work_progress
# or just delete (it's gitignored, so no `git rm`):
rm -rf logs/work_progress
```

### 6. Confirm + delete `models/archive/backup_pre_*/` (open question)

If Luis confirms nothing outside git history needs them:

```bash
git rm -r models/archive/backup_pre_expand_20260719_164210
git rm -r models/archive/backup_pre_h13_retrain_20260719_083506
git rm -r models/archive/backup_pre_htf_history_expand_20260719_170000
# leaves models/archive/ empty; either keep as a stub for future
# archived model families, or:
rmdir models/archive  # only if empty
```

### 7. Sweep the git-plumbing detritus

```bash
git gc --prune=now
# removes stale .git/objects/*/tmp_obj_* automatically.
```

### 8. Decide on the pre-cleanup / pre-scorecard tags

If they no longer serve a purpose:

```bash
git tag -d beta-1.10.15-pre-forge-scorecard
git tag -d beta-1.10.16-pre-cleanup
git tag -d beta-1.10.20-pre-cleanup
```

(Keep them if you want a rollback marker; otherwise clean tag list.)

### 9. Decide on `docs/reporting/{LOGGING_SPEC,REPORT_TEMPLATE}.md`

If Forge scoring (§7.10) fully supersedes them:

```bash
git mv docs/reporting/LOGGING_SPEC.md docs/archive/
git mv docs/reporting/REPORT_TEMPLATE.md docs/archive/
rmdir docs/reporting  # only if empty
```

Otherwise keep -- the risk is low.

### 10. Add a `logrotate.d/predictor` config (not clutter, but bundled with the cleanup)

Not in this scope, but the "logs grow forever" gap flagged in
`DATA_INVENTORY.md` §7.5 belongs alongside the cleanup work. Five-minute
addition to `install.sh` writing an `/etc/logrotate.d/predictor` file that
targets `/opt/predictor/logs/*.log`.
