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
- [x] Executor token auth for mutating routes: verified no frontend/internal caller currently depends on nginx POST; `/execute`, `/cancel/*`, and `/close/*` are token-gated in repo.
- [x] Predictor token auth for service mutating endpoint: verified signal_agent is the service caller for enriched-signal; `/api/enriched-signal/*` is token-gated in repo.
- [ ] Public chat abuse control: `/api/chat` is browser-facing and intentionally not token-gated in Beta 1 so disabled-ready UI keeps returning honest `503 agent_unavailable`; add rate limit/session auth later if chat is enabled publicly.
- [x] Live-mode double gate: executor now forces dry-run unless `DRY_RUN=false` and `LIVE_CONFIRM=I_ACCEPT_LIVE_TRADING` are both set.
- [ ] Secret scoping per container: verify which services actually need `.env` values before removing `env_file` broadly.
- [x] CORS cleanup: default origins now limited to `http://localhost,http://127.0.0.1`, configurable with `DASHBOARD_ORIGINS` for LAN/VPS.
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
