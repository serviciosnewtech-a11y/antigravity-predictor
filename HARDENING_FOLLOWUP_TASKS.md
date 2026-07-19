# Antigravity Predictor — Hardening Follow-up Tasks

**Purpose:** Track Fable 5 / third-party audit findings and runtime discoveries without blindly implementing them. Items below require source/runtime verification before repo changes.

**Current boundary:**
- Target/local Hermes handles configuration, install execution, runtime checks, logs, and blocker reporting only.
- Target/local Hermes must not patch core application code, hardening logic, model logic, dashboard code, Dockerfiles, or repo source files.
- Core/code issues are fixed only through controller/repo workflow and pushed for the next pull.

---

## Addressed in installer/deploy hardening

- [x] Remove default host port publishing for backend services `predictor`, `executor`, and `forge` in `docker-compose.yml`.
- [x] Update `deploy.sh` to verify nginx-routed endpoints instead of direct backend host ports.
- [x] Update `diagnose.sh` to report backend host-port exposure explicitly.
- [x] Update target-Hermes prompt/dossier/log so install agents report blockers and do not patch source.

---

## Verify before implementing

- [ ] Public repo hygiene/scrub decision: inspect public docs for personal email, internal paths, stale private claims, and operational handoffs.
- [ ] Executor token auth for mutating routes: verify current routes/callers first; ensure token never reaches frontend JS.
- [ ] Predictor token auth for mutating/compute endpoints: verify current `/api/chat` and enriched-signal caller flow first.
- [ ] Live-mode double gate: verify executor live/dry-run handling before patching.
- [ ] Secret scoping per container: verify which services actually need `.env` values before removing `env_file` broadly.
- [ ] CORS cleanup: verify dashboard origin and local/LAN deployment modes before tightening.
- [ ] Nginx rate limiting: verify request patterns so polling/WebSocket handshakes are not broken.
- [ ] WebSocket exposure/auth: verify actual `/ws` route and dashboard behavior after backend port removal.
- [ ] Persistent execution audit log: verify existing `ExecutionRecord` lifecycle before adding JSONL persistence.
- [ ] Container hardening: test `cap_drop`, `read_only`, tmpfs, and non-root users service-by-service.
- [ ] Dependency pinning/reproducibility: decide functional vs deterministic rebuild target.
- [ ] Log hygiene: scan runtime logs after real test cycle for tokens/prompts/secrets.
- [ ] Order sizing safety: verify `calc_order_size()` behavior with exchange min qty and small balances.
- [ ] Exchange-side SL/TP: design/test only on testnet or dry-run abstraction before live.
- [ ] Confidence threshold reconciliation: collect realtime signal/executor skip data before recalibration.
- [ ] Feature completeness: verify live feature vector gaps against training feature set before claiming AUC validity.
- [ ] Rollback rehearsal: perform after one clean install baseline exists.

---

## Runtime evidence intake format

```text
ISSUE=<short name>
SOURCE=<target Hermes | controller | user | runtime log>
COMMIT=<git rev-parse HEAD>
SYMPTOM=<exact observed symptom>
EVIDENCE=<command output/log excerpt/path>
CLASS=<installer/config | core code | docs | hardening | model/data>
OWNER=<target config/report | controller repo fix | operator decision>
NEXT=<one concrete next action>
```
