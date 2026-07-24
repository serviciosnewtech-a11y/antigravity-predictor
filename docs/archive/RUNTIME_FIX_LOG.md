# Antigravity Predictor — Runtime Fix Log

**Purpose:** Keep a detailed, evidence-backed log while we get the app running with the bare minimum fixes. Future hardening and correctness work should be driven by real runtime evidence, not hypothetical cases.

**Repo:** https://github.com/serviciosnewtech-a11y/antigravity-predictor

**Current main commit at log start:** `bce7598eefe29a6f034a0299e5e7fd9a4852e3ce`

**Operating posture:**
- Fix the minimum required to get the test deployment running.
- Target/local Hermes is configuration/install/runtime-reporting only; it must not patch core repo code or diagnose by editing source files.
- Core issues found on the test machine are reported back as evidence and fixed by the controller/repo workflow.
- Keep `DRY_RUN=true` and exchange keys empty.
- Do not enable live trading.
- Do not apply ad-hoc host patches or source-code patches on the test machine unless explicitly approved.
- Source fixes should be committed at repo level and deployed by pull/rebuild.
- Prefer real runtime evidence over hypothetical hardening changes.
- Record blockers, commands, outputs, and follow-up items here so repeated realtime runs can be streamlined.

---

## Log format for each runtime run

```text
## Run YYYY-MM-DD HH:MM <host>

Goal:

Source state:
- commit:
- branch:
- repo visibility:

Environment:
- host:
- install path:
- Docker access: direct | sg docker | blocked
- DRY_RUN:
- EXCHANGE keys empty:
- SA_INFERENCE_BACKEND:

Commands run:
- command:
  result:
  evidence:

Observed failures:
- symptom:
  exact output:
  suspected cause:
  owner: controller | target Hermes | repo fix | operator

Configuration/install fixes applied by target agent:
- fix:
  file/path:
  reason:
  verification:

Core/code fixes required upstream:
- issue:
  evidence:
  repo file/path if known:
  owner: controller/repo workflow

Verification:
- docker compose ps:
- /api/status:
- :18910/api/status:
- :18911/health:
- :18912/health:
- browser/UI smoke:

Deferred follow-up:
- item:
  why deferred:
  evidence needed:

Outcome:
- PASS | BLOCKED | PARTIAL
- next action:
```

---

## Run 2026-07-18 controller-pre-test

Goal:
Prepare the repo for target-machine, agent-assisted install testing with minimum viable deployment posture.

Source state:
- commit: `bce7598eefe29a6f034a0299e5e7fd9a4852e3ce`
- repo: https://github.com/serviciosnewtech-a11y/antigravity-predictor
- visibility: public, verified by anonymous ls-remote and clone smoke

Evidence already captured:
- `make build`: PASS before push and after UI/dossier changes
- anonymous clone smoke: PASS
- `INSTALL_DOSSIER_FOR_HERMES.md`: present in fresh clone
- `deploy.sh`: present/executable in fresh clone

Current agreed posture:
- Run agent-assisted install on test machine from GitHub.
- Target/local Hermes may adjust configuration/install steps only within the approved brief.
- Target/local Hermes must not patch core app issues; it reports evidence so fixes can be made at repo level by the controller workflow.
- Use the hardening backlog as follow-up guidance, not as the immediate implementation scope.
- Record realtime failures before deciding which hardening/correctness items to implement.

Deferred follow-up queue:
- Public repo hygiene decision/scrub after current test cycle.
- Hardening Backlog v2 P0/P1 implementation after runtime baseline is proven.
- Verified installer/deploy hardening now includes backend service host-port removal and nginx-routed health checks; remaining Fable 5 audit items stay advisory until verified against source/runtime.
- Batch installer creation after one or more manual/agent-assisted installs reveal real friction.
- Realtime run evidence collection for chat behavior, signal behavior, data pipeline gaps, and Docker/runtime issues.

Outcome:
- READY_FOR_TARGET_AGENT_INSTALL_TEST
- Next action: paste the single-block target Hermes prompt into the test-machine Hermes and return its final report.

---

## Run 2026-07-19 beta1-controller-hardening

Goal:
Implement verified minimum repo-level Beta 1 hardening before the next install pull, while keeping target/local Hermes config/install/report-only.

Source state:
- base commit: `e1fde76905d37780cadc909bde2d6e7321fc399b`
- repo: https://github.com/serviciosnewtech-a11y/antigravity-predictor

Backups:
- local backup root: `/media/hermes/Storage/products/Predictor/backups/beta1-20260719T010212Z`
- backup directory is local-only and ignored by git.

Repo-level changes staged for Beta 1:
- token-gate executor mutating routes: `/execute`, `/cancel/*`, `/close/*`
- token-gate predictor service mutating route: `/api/enriched-signal/*`
- leave browser-facing `/api/chat` available so disabled backend returns honest `503 agent_unavailable`; public chat abuse control remains deferred
- wire signal_agent to send `X-Internal-Token` to predictor
- generate `INTERNAL_API_TOKEN` in `deploy.sh` when `.env` is missing or blank
- force dry-run unless `DRY_RUN=false` and `LIVE_CONFIRM=I_ACCEPT_LIVE_TRADING`
- replace wildcard CORS defaults with configurable `DASHBOARD_ORIGINS`
- deny public nginx POSTs through `/executor/`, preserving read-only status through nginx

Deferred follow-up:
- rate limiting, container hardening, dependency pinning, log hygiene, audit log persistence, order sizing, SL/TP, threshold/data-feature work remain in `HARDENING_FOLLOWUP_TASKS.md` until verified by runtime evidence.
