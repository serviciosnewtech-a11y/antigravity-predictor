# Antigravity Predictor

Antigravity Predictor ships as two separate, self-contained installers built from the same core app (`src/`, `dashboard/`, `models/`, `data/`). Pick one — they don't mix:

- **Docker** — everything under `deploy/docker/` (compose file, Dockerfiles, launcher). Requires a working Docker daemon.
- **Bare metal** — everything under `deploy/bare-metal/` (VPS installer, systemd units, nginx config) plus the repo-root `run_monolith.sh`/`run_local.sh` launchers. No Docker involved at all.

## Quick deploy — Docker

```bash
git clone https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
cd antigravity-predictor
bash deploy/docker/deploy.sh
```

If deploying from a release tarball instead of GitHub:

```bash
mkdir -p ~/antigravity-predictor-test
cd ~/antigravity-predictor-test
tar -xzf /path/to/antigravity-predictor-release.tar.gz
sha256sum -c SHA256SUMS.txt
bash deploy/docker/deploy.sh
```

`deploy/docker/deploy.sh` creates `.env` from `.env.example` if missing, keeps `DRY_RUN=true`, verifies Docker access, starts the stack, and smoke-checks the dashboard and APIs.

## Quick deploy — bare metal (no Docker)

```bash
bash run_monolith.sh
```

boots the predictor, executor, forge, and signal-agent processes directly on the host. For a permanent systemd + nginx VPS install, run `sudo bash deploy/bare-metal/install.sh` instead.

## Language support

The dashboard Hermes chat panel includes an EN/ES toggle.

Spanish mode translates the client-facing chat UI and sends `language: "es"` to `/api/chat`, so configured agent backends are instructed to answer advisory text in Spanish.

## Hermes target-agent deployment

A test PC with Hermes installed can act as the deployment worker. Use:

```text
TARGET_HERMES_DEPLOY_PROMPT.md
```

Paste that prompt into Hermes on the target PC, replacing the tarball path and SHA256 placeholders.

## Technical dossier and paper-evaluation planning

- `ANTIGRAVITY_PREDICTOR_BETA1_FULL_TECHNICAL_DOSSIER.md` — Beta 1 technical dossier, 15m trading intent, dry-run baseline values, and SOUL-ready operating boundaries.
- `docs/reporting/LOGGING_SPEC.md` — proposed append-only paper-evaluation logging spec.
- `docs/reporting/REPORT_TEMPLATE.md` — proposed weekly paper report template.
- `docs/plans/BETA_1_1_LOGGING_IMPLEMENTATION_PLAN.md` — implementation plan for adopting the logging/reporting spec without changing Beta 1 runtime behavior.

## Diagnose

```bash
bash deploy/docker/diagnose.sh
```

Return the full output if deployment is blocked. (Docker path only — bare metal has no equivalent script; check `journalctl -u predictor -f` and `curl localhost:18910/api/status` instead.)

## Default inference

The default client demo is LLM/agent agnostic. It does not require Hermes, Anthropic, Ollama, or a target-side agent:

```env
SA_INFERENCE_BACKEND=disabled
```

In this mode the dashboard, market display, forecasts, paper-trade controls, and signal context remain available. Chat/enrichment must show an honest unavailable/disabled state if no backend exists; it must not fake replies.

Optional Hermes/OpenAI-compatible enrichment can be enabled later:

```env
SA_INFERENCE_BACKEND=openai_compatible
HERMES_PROXY_URL=http://host.docker.internal:8645/v1
HERMES_INFERENCE_MODEL=<operator-approved-model>
HERMES_PROXY_API_KEY=local
```

Optional Ollama can also be used when available from Docker:

```env
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=<operator-approved-ollama-model>
```

Do not treat Hermes, Ollama, Anthropic, or any local agent as required for the default deploy path.

## Safety

Demo deployment defaults to:

```env
DRY_RUN=true
EXCHANGE_API_KEY=
EXCHANGE_API_SECRET=
```

Do not set `DRY_RUN=false` until exchange credentials, risk controls, and production environment are intentionally configured.
