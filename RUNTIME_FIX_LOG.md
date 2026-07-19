# Antigravity Predictor — Runtime Fix Log

**Purpose:** Keep a detailed, evidence-backed log while we get the app running with the bare minimum fixes. Future hardening and correctness work should be driven by real runtime evidence, not hypothetical cases.

**Repo:** https://github.com/serviciosnewtech-a11y/antigravity-predictor

**Current main commit at log start:** `b48f1dce395076122b1a9261e648e2c7e55f5de7`

**Operating posture:**
- Fix the minimum required to get the test deployment running.
- Keep `DRY_RUN=true` and exchange keys empty.
- Do not enable live trading.
- Do not apply ad-hoc host patches on the test machine unless explicitly approved.
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

Bare-minimum fixes applied:
- fix:
  file/path:
  reason:
  verification:

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
- commit: `b48f1dce395076122b1a9261e648e2c7e55f5de7`
- repo: https://github.com/serviciosnewtech-a11y/antigravity-predictor
- visibility: public, verified by anonymous ls-remote and clone smoke

Evidence already captured:
- `make build`: PASS before push and after UI/dossier changes
- anonymous clone smoke: PASS
- `INSTALL_DOSSIER_FOR_HERMES.md`: present in fresh clone
- `deploy.sh`: present/executable in fresh clone

Current agreed posture:
- Run agent-assisted install on test machine from GitHub.
- Fix only what blocks the app from running in safe test mode.
- Use the hardening backlog as follow-up guidance, not as the immediate implementation scope.
- Record realtime failures before deciding which hardening/correctness items to implement.

Deferred follow-up queue:
- Public repo hygiene decision/scrub after current test cycle.
- Hardening Backlog v2 P0/P1 implementation after runtime baseline is proven.
- Batch installer creation after one or more manual/agent-assisted installs reveal real friction.
- Realtime run evidence collection for chat behavior, signal behavior, data pipeline gaps, and Docker/runtime issues.

Outcome:
- READY_FOR_TARGET_AGENT_INSTALL_TEST
- Next action: paste the single-block target Hermes prompt into the test-machine Hermes and return its final report.
