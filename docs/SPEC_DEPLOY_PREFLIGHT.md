# SPEC — Deployment Preflight Gate

**Stage:** SPEC (queued behind Packet C; independent of it)
**Purpose:** replace subjective "green for deployment" ratings with a machine-produced evidence file
**Date:** 2026-07-26

---

## 0. Why previous deployments broke despite being rated green

The record in `HANDOFF.md`, `LIVE_DEPLOY_NOTES.md`, `docs/archive/RESCUE_HANDOFF.md` and the
git log shows twelve distinct recurring failure modes. Not one of them is a code-quality
defect. **Every one is a property of the target machine or the artifact, not of the source.**

A model reading the repository cannot observe any of them. So a "green" rating derived from
reading code was never evidence about deployability — it was a category error, and it was
right to abort on it.

Two structural gaps make this concrete:

- **No bare-metal preflight exists.** `deploy/docker/deploy_target_smoke.sh` is docker-only.
  The entire `deploy/bare-metal/install.sh` path (543 lines, root, 9 systemd units, nginx,
  ufw, htpasswd) has no pre-checks whatsoever. `verify.sh` runs *after* install — it tells
  you the deploy failed, not that it would.
- **The existing smoke mutates the target.** It runs `up -d --build` and *then* curls health.
  A preflight that changes the machine cannot be run safely before deciding to deploy.

**Definition adopted here:** "green" is `reports/preflight_{host}_{timestamp}.json` with
`"blockers": []`, produced **on the target host**, against a named artifact SHA256, within
60 minutes of the deploy attempt. Nothing else is green. No model, including me, may assert
deployment readiness without that file.

---

## 1. Design rules

1. **Read-only and idempotent.** The preflight installs nothing, starts nothing, writes only
   its own report. Safe to run repeatedly, safe to run on a production host.
2. **Runs on the target, not on the build machine.** Every check below is unobservable from
   the repo.
3. **Emits evidence, not verdicts.** Each check records the observed value, not just pass/fail.
4. **Exit code = number of blockers.** Same convention as the existing `verify.sh`.
5. **Named blockers.** Each failure cites the incident class it prevents, so the report is
   traceable to the history rather than being a generic checklist.

---

## 2. Checks, derived from the actual failure record

Ranked by frequency of occurrence. Each cites the incident it exists to prevent.

### BLOCKER class — deploy must not proceed

**P-01 — sudo / privilege state**
`docs/ISSUE_TEMPLATE/deploy-blocker.md` calls sudo state "the #1 root cause of deploy
blockers"; `docs/DEPLOY_NONINTERACTIVE.md` notes locked-down hosts frequently cannot do it.
Check: `id -u`, `sudo -n true` exit status, presence of `/etc/sudoers.d/hermes-deploy`,
whether the invoking account is in a group granting the needed rights. Record all four.
Blocker if install path requires root and non-interactive sudo is unavailable.

**P-02 — cwd-independent path resolution**
Highest-frequency runtime failure, 4+ separate incidents: `GOLD_PARQUET_PATH` resolving to
`<APP_DIR>/src/data/...` after `run.sh` cd's into `src/` (XAU 503s, `HANDOFF.md:1082`);
`ModuleNotFoundError: feature_gate`; commit `67ecaf8` "cwd-dependent model path resolution —
real bare-metal install crash (exit status 3)".
Check, statically: grep the shipped tree for relative-path defaults in config and source
(`data/`, `models/`, `src/`) that are resolved without anchoring to a module-relative root.
Check, dynamically: import-and-resolve the server's path helpers from three working
directories — repo root, `src/`, and `/` — and assert identical absolute results.
Blocker on any divergence.

**P-03 — venv integrity**
`/opt/predictor/.venv` was never created, `predictor.service` crash-looped on `203/EXEC`,
root cause still open (`HANDOFF.md:317-333`, §7.7). Separately, `.venv` excluded from
tarball broke cold `./run.sh` (`HANDOFF.md:1084`).
Check: `python3 -m venv --help` succeeds (python3-venv actually installed, not just python3),
interpreter version ∈ {3.10, 3.11, 3.12}, target venv path either absent or containing a
working interpreter that can `import lightgbm, fastapi, uvicorn`. Record the resolved
interpreter path and version. Blocker on a venv that exists but cannot import.

**P-04 — APP_DIR permissions**
Mode `0700` on `$APP_DIR` "blocked three different things run by a different account"
(`HANDOFF.md:342-356`); `ProtectSystem=strict` plus a `d---------` directory produced
`exited with code 126: Permission denied` (`LIVE_DEPLOY_NOTES.md` #5).
Check: `$APP_DIR` mode, owner, group, ACL (`getfacl`), and whether the service user can
traverse every parent. Blocker if the service account cannot read the app dir.

**P-05 — artifact identity**
"Version-in-git-tags-vs-tarball drift... retries stacked" (`HANDOFF.md` §2); `beta-1.11`
sorts after but is chronologically older than `beta-1.10.32`.
Check: tarball SHA256 matches the `.sha256` sidecar **and** matches the SHA recorded in the
deploy manifest for the intended tag. Record both. Blocker on mismatch. Never resolve a
version by lexical tag sort.

**P-06 — model and data files present under canonical names**
`RESCUE_HANDOFF.md` "Bug D — model files in `models/` canonical names vs dated names".
Check: all six `models/model_{btc,eth,sol}_{long,short}.txt` present in the artifact, each
parses as a LightGBM booster, and — new, from the v1.11.0 incident — record each file's
`internal_count` and `max_feature_idx` in the report. Blocker if any is missing, or if
`max_feature_idx + 1` does not equal the feature count the server expects.
Also check `data/macro/gold.parquet` exists.

**P-07 — .env and systemd environment plumbing**
`predictor.service` had no `EnvironmentFile=`, so nothing in `.env` reached `/api/chat`
(`HANDOFF.md` §3, commit `6435c3b`); ".env can silently be empty/wrong-owned"
(`LIVE_DEPLOY_NOTES.md` #1, #2).
Check: `.env` exists, non-empty, owner `predictor:predictor`, mode 600; and every systemd
unit that consumes it declares `EnvironmentFile=`. Blocker on a unit reading env vars
without the directive.

**P-08 — port availability**
`config.json` sed `0.0.0.0`→`127.0.0.1` "didn't fire" is an explicit handoff check; port 80
collision is an explicit STOP in the docker smoke.
Check: 80, 443, 18910, 18911, 18912 — occupied or free, and by which PID. Record the
resolved bind address from the shipped `config.json`. Blocker if a required port is held, or
if a backend port would bind `0.0.0.0` on a host-exposed interface.

### WARNING class — record, do not block

**P-09 — network egress.** Reachability of `api.bybit.com` (REST + WebSocket), PyPI,
yfinance, `nfs.faireconomy.media`, RSS feeds. Egress-blocked hosts degrade rather than fail
outright, but the report must say so up front instead of surfacing as 503s later.

**P-10 — systemd PATH.** `agent_binary_exists: false` for a binary that works interactively
(`LIVE_DEPLOY_NOTES.md` #4). Check that every binary referenced in a unit resolves by
absolute path, not bare name.

**P-11 — unverified shipped defaults.** `AGENT_RELAY_CMD='hermes --profile metis chat -q
{prompt}'` "was never actually verified on any host" (`LIVE_DEPLOY_NOTES.md` #7, commit
`5976286`); the `{prompt}` quoting trap passes health check but fails real use (#6). Check
the configured relay binary exists at an absolute path; record it. Do not execute it.

**P-12 — disk and OS.** ≥4 GB free on the `$APP_DIR` filesystem; record `/etc/os-release`.
Ubuntu 22.04 is the only tested target (`docs/REMOTE_DEPLOY_HANDOFF.md`).

**P-13 — hardcoded absolute paths from the build host.**
`models/metadata.json:15` contains `/media/hermes/Storage/Antigravity/...`. Harmless today
(metadata only), but scan the artifact for `/media/hermes/Storage` and report every hit —
one of them will eventually be load-bearing.

---

## 3. Deliverables

- `tools/preflight.sh` — read-only, runs on target, no dependencies beyond coreutils,
  python3, ss/getfacl. Exit code = blocker count.
- `reports/preflight_{host}_{timestamp}.json` — per check: `id`, `status`
  (`pass`/`warn`/`blocker`), `observed` (the actual value), `incident_ref`.
- `docs/DEPLOY_GATE.md` — states the green definition in §0 and that no deploy proceeds
  without a zero-blocker report from the target.

## 4. Sequencing

Preflight is written and validated **on the current working host first**, where the known
failure modes can be reproduced deliberately. Only once it correctly flags a known-bad
configuration does it get trusted on the test machine.

Order: Packet C completes → Stage 1 models validated or retracted → preflight built →
preflight run on test machine → deploy only on a zero-blocker report.

Do not deploy Stage 1 models that have not cleared Gate C. A clean preflight means the
machine will run the software; it says nothing about whether the model is worth running.
