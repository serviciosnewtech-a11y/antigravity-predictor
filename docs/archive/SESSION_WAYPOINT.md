# Antigravity Predictor — Session Waypoint
**Written:** 2026-07-18 (end of night session)  
**Next action:** SSH smoke test on Linux Mint test machine

---

## State of the Build

Everything is coded and audited. Nothing is broken on disk. The deployment has **not yet been tested on a remote machine** — that is Task #47, scheduled for tomorrow.

### What is complete

| Area | Status |
|---|---|
| LightGBM models (6 × .txt) | ✅ Trained, AUC 0.841–0.864, in `models/` |
| predictor_server.py | ✅ FastAPI, WebSocket, `/api/chat`, enriched-signal endpoints |
| signal_agent (Hermes) | ✅ Docker service, polls predictor, fires LLM enrichment at threshold 0.22 |
| Dashboard Hermes chat FAB | ✅ Floating chat panel wired to `/api/chat` |
| All 5 Docker services + retrain | ✅ Dockerfiles audited, paths verified |
| docker-compose.yml | ✅ 5 always-on services + 1 profile-based retrain |
| .env.example | ✅ Complete, all SA_ vars present |
| DOSSIER.md | ✅ Fully rewritten (2026-07-18) |
| retrain_all.sh | ✅ MIN_AUC default fixed (0.54), Docker-aware predictor reload |

### Known minor items (non-blocking)

- After a Docker retrain, `docker compose restart predictor` must be run manually. The script now writes a `.retrain_complete` sentinel and logs the instruction.
- No TLS yet (certbot / domain). HTTP only for test deployment.
- No GitHub Actions CI.

---

## Task #47 — SSH Smoke Test

**Machine:** Linux Mint (SSH ready)  
**Goal:** `docker compose up -d --build` completes, all 5 services healthy, dashboard loads.

### Pre-requisites on the test machine

```bash
# 1. Docker Engine (not Docker Desktop)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # then re-login

# 2. Docker Compose plugin (included with modern Docker Engine, verify)
docker compose version

# 3. Ollama (for signal_agent LLM enrichment)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1
# Ollama must be running before `docker compose up`
# It starts automatically as a systemd service after install
```

### Deployment commands

```bash
git clone https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
cd antigravity-predictor

cp .env.example .env
# No edits needed for smoke test — default SA_INFERENCE_BACKEND=ollama

docker compose up -d --build
```

### Verification checklist

```bash
# All 5 services should show "Up"
docker compose ps

# Predictor API (through nginx)
curl -s http://localhost/api/status | python3 -m json.tool

# Predictor API (direct, bypassing nginx)
curl -s http://localhost:18910/api/status | python3 -m json.tool

# Dashboard should load in browser
# Open: http://localhost/

# Signal agent enrichment activity
docker compose logs -f signal_agent

# Predictor model load + WebSocket
docker compose logs -f predictor
```

### What a passing test looks like

- `docker compose ps`: all 5 services `Up`, no `Restarting`
- `curl http://localhost/api/status`: JSON with BTC/ETH/SOL signal blocks
- Browser: dashboard loads, candlestick chart renders, Hermes FAB visible bottom-right
- `signal_agent` logs: `Polling predictor...` every 30s — no crash loops
- `predictor` logs: `Models loaded — 126 features` for each of BTC/ETH/SOL

### If a service is crashing

```bash
docker compose logs <service_name> --tail 50
```

Most likely failure modes:
- **predictor** crash: model path wrong in config.json, or missing bind mount
- **signal_agent** crash: can't reach predictor (timing — add `depends_on` health check if needed)
- **dashboard** crash: nginx.conf proxy target not resolving (network naming)

---

## Architecture Reminder (for fresh context)

```
Client → port 80 (nginx/dashboard) → proxies /api/ and /ws to predictor:18910
signal_agent polls predictor:18910 every 30s, fires LLM when prob > 0.22
Ollama runs on HOST, reachable from containers via host.docker.internal:11434
```

---

## File Locations

| File | Purpose |
|---|---|
| `DOSSIER.md` | Full architecture reference |
| `docker-compose.yml` | Deployment definition |
| `.env.example` | All env vars with comments |
| `retrain_all.sh` | Full retrain pipeline |
| `src/predictor_server.py` | FastAPI server (chat endpoint, enriched-signal) |
| `src/signal_agent/config.py` | Hermes config (threshold = 0.22) |
| `signal_agent/Dockerfile` | Hermes container |

---

*Waypoint written by Claude (Cowork mode) — 2026-07-18*
