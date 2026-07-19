# Antigravity Predictor — Hermes Test-Machine Install Dossier

**Purpose:** Anchor a target-side Hermes agent so it performs one bounded install test without redesign, scope drift, unsafe trading behavior, or ambiguous recovery loops.

**Target product:** Antigravity Predictor

**Repository page:** `https://github.com/serviciosnewtech-a11y/antigravity-predictor`

**Clone URL:** `https://github.com/serviciosnewtech-a11y/antigravity-predictor.git`

**Verified source:** latest public `main` or the `beta-1` tag after Beta 1 publication.

**Install root:** `~/antigravity-predictor-test/antigravity-predictor`

**Default mode:** safe demo / paper mode only.

---

## 0. Operating contract for target Hermes

You are the **execution worker on the test machine**.

Do exactly this install test. Do not redesign, refactor, simplify, replace services, change the product architecture, or invent alternative deployment paths unless the documented path is blocked by concrete evidence.

Authority boundary: target/local Hermes may handle configuration, install execution, runtime checks, logs, and blocker reporting only. It must not patch core application code, hardening logic, model logic, dashboard code, Dockerfiles, or repo source files. Core issues found during the run are reported back with evidence and fixed through the controller/repo workflow.

The product must remain deployable without requiring Hermes, Anthropic, Ollama, OpenAI, or any target-side agent as a hard dependency. Hermes on the target machine is only the installer/verifier. The app's optional chat/enrichment backend may stay disabled.

### Hard stop rules

Stop immediately and report evidence if any of these are true:

1. `DRY_RUN` is not `true`.
2. `EXCHANGE_API_KEY` or `EXCHANGE_API_SECRET` is non-empty.
3. Docker is not installed or not accessible through the current user or `sg docker`.
4. Required public entry port is held by an unrelated service and the existing listener is not this app stack:
   - `80`
5. Any step requires sudo, destructive host changes, wiping unknown directories, firewall changes, package installation, credential entry, or production exchange setup.
6. The GitHub clone does not resolve to the latest public `main`, the `beta-1` tag, or a specific operator-approved commit.
7. A command fails and the next action is not explicitly covered by this dossier or by `diagnose.sh`.

Do not continue under uncertainty. Return a concise blocker report.

---

## 1. Expected target machine prerequisites

Required:

```bash
git --version
docker --version
docker compose version
curl --version
ss --version || command -v ss
```

Docker access must work by one of these:

```bash
docker ps
```

or:

```bash
sg docker -c 'docker ps'
```

If neither works, stop with:

```text
BLOCKED: docker socket inaccessible
```

Do not install Docker or change user groups unless the operator explicitly asks.

---

## 2. Exact install commands

Run on the test machine:

```bash
set -euo pipefail
mkdir -p ~/antigravity-predictor-test
cd ~/antigravity-predictor-test

if [ -d antigravity-predictor/.git ]; then
  cd antigravity-predictor
  git fetch origin main
  git checkout main
  git pull --ff-only origin main
else
  git clone https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
  cd antigravity-predictor
fi

git rev-parse HEAD
```

Expected source:

```text
latest public main, or a specific commit explicitly approved by the operator
```

Then:

```bash
bash deploy.sh
```

Do not manually edit `.env` before the first deploy. `deploy.sh` creates `.env` from `.env.example` if missing and enforces safe demo mode.

---

## 3. Safety expectations before deploy

After `deploy.sh` creates `.env`, these must be true:

```env
DRY_RUN=true
EXCHANGE_API_KEY=
EXCHANGE_API_SECRET=
SA_INFERENCE_BACKEND=disabled
```

The default app mode is intentionally LLM/agent agnostic. If chat/enrichment has no backend, it must show an honest unavailable/disabled state. It must not fake replies.

Do not enable:

```env
DRY_RUN=false
```

Do not add exchange credentials.

Do not enable Hermes/Ollama/Anthropic enrichment during the first install test unless the core Docker app is already green.

---

## 4. What `deploy.sh` is expected to do

`deploy.sh` should:

1. create `.env` from `.env.example` if `.env` does not exist
2. load `.env`
3. block if demo safety is violated
4. skip Hermes proxy startup when `SA_INFERENCE_BACKEND=disabled`
5. verify Docker/Compose access
6. allow redeploy if public port `80` is already owned by this same app stack
7. block if public port `80` is owned by an unrelated service
8. run:

```bash
docker compose config --quiet
docker compose up -d --build
```

or equivalent through `sg docker`

9. wait for these nginx-routed endpoints:

```text
http://localhost/api/status
http://localhost/executor/health
http://localhost/forge/health
```

Backend services should not publish host ports in the hardened installer path. Direct host checks for `18910`, `18911`, and `18912` are diagnostic exposure checks, not required success endpoints.

10. print the dashboard URL:

```text
Dashboard: http://<LAN_IP>/
```

---

## 5. Success criteria

The install test is successful only if all checks pass:

```bash
docker compose ps
curl -fsS http://localhost/api/status
curl -fsS http://localhost/executor/health
curl -fsS http://localhost/forge/health
```

Expected minimum dashboard API response:

```json
{"status":"online"}
```

Exact JSON may include more fields. Do not fail solely because extra fields exist.

Browser/UI smoke:

1. Open the dashboard URL printed by `deploy.sh`.
2. Confirm dashboard loads.
3. Confirm no live-trading activation is required.
4. Confirm Spanish/English chat UI toggle is visible.
5. Confirm default chat/enrichment behavior is honest:
   - either explicitly unavailable/disabled, or
   - a real configured backend reply if the operator separately enabled one later.
6. Confirm there is no fake/demo chat response pretending to be an LLM.

---

## 6. Required final report from target Hermes

Return only concrete evidence in this shape:

```text
INSTALL_RESULT=<PASS|BLOCKED>
HOST=<hostname>
PWD=<absolute install path>
COMMIT=<git rev-parse HEAD>
DRY_RUN=<value from .env>
EXCHANGE_KEYS_EMPTY=<yes|no>
INFERENCE_BACKEND=<value from .env>
DOCKER_ACCESS=<direct|sg docker|blocked>
DASHBOARD_URL=<url printed by deploy.sh>

COMMANDS_RUN:
- <command 1>
- <command 2>

SERVICE_STATE:
<docker compose ps output>

STATUS_ENDPOINTS:
/dashboard_proxy=<curl http://localhost/api/status output>
/executor_health_proxy=<curl http://localhost/executor/health output>
/forge_health_proxy=<curl http://localhost/forge/health output>
/backend_host_ports=<diagnose.sh backend_ports_exposed value>

UI_SMOKE:
loads=<yes|no|not tested>
language_toggle_visible=<yes|no|not tested>
chat_behavior=<disabled honest|real backend reply|fake/invalid|not tested>

BLOCKERS:
- <only if blocked>

NEXT_ACTION:
- <one concrete next action only>
```

Do not summarize success without the command outputs above.

---

## 7. Blocker workflow

If `bash deploy.sh` fails:

```bash
bash diagnose.sh
```

Return the full `diagnose.sh` output.

Do not patch code on the test machine unless the operator explicitly asks. The test machine is for configuration, install verification, runtime checks, and blocker reporting. Source changes belong in the repo workflow and must be pushed from the controller side after diagnosis.

---

## 8. Known acceptable warnings / non-blockers

These are not blockers by themselves:

- pip warning about running as root inside Docker build
- Docker build downloading Python packages
- existing public port `80` if the existing listener is the same Antigravity/Predictor Docker stack and the nginx-routed health endpoints respond
- chat/enrichment disabled in default mode
- no Hermes proxy on port `8645` when `SA_INFERENCE_BACKEND=disabled`

---

## 9. Known blockers / do not paper over

These are blockers until fixed or explicitly overridden by the operator:

- Docker socket inaccessible
- `docker compose config --quiet` fails
- any required health endpoint remains unavailable after deploy wait
- `DRY_RUN=false`
- exchange credentials are present during demo test
- unrelated service owns public port `80`
- backend host ports `18910`, `18911`, or `18912` remain exposed after hardened redeploy, unless the operator explicitly approved loopback-only dev exposure
- dashboard loads with fake assistant replies instead of disabled/real backend state
- git clone asks for credentials for the public repo
- commit does not match the expected commit or a newer operator-approved commit

---

## 10. Optional post-core enrichment, not part of first install test

Only after the core app passes, the operator may choose to enable optional LLM enrichment.

Hermes/OpenAI-compatible example:

```env
SA_INFERENCE_BACKEND=openai_compatible
HERMES_PROXY_URL=http://host.docker.internal:8645/v1
HERMES_INFERENCE_MODEL=<operator-approved-model>
HERMES_PROXY_API_KEY=local
```

Then rerun:

```bash
bash deploy.sh
```

This is a separate test. Do not mix optional enrichment debugging with first-pass core deployment.

---

## 11. One-line mission anchor

Install the pushed Docker app from GitHub, keep it in safe demo mode, verify live endpoints and visible dashboard behavior, and stop on any ambiguity instead of redesigning or guessing.
