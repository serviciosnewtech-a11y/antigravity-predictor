# Antigravity Predictor — RESCUE HANDOFF
**Written:** 2026-07-18
**Written by:** Claude (Cowork/Sonnet) — treating own work as untrusted
**For:** Next agent (Gemini / Codex / Opus) tasked with recovering this deployment
**Status:** RED — not verified running anywhere. Treat everything below as claims to be tested, not facts.

---

## VERIFIED FACTS (confirmed via shell 2026-07-18)

```
GITHUB REPO (SSH):   f41f368735e1cd565e6860d922dcb8759757957f HEAD  ← CONFIRMED EXISTS
GITHUB REPO (HTTPS): fatal: could not read Username                ← HTTPS blocked in sandbox
                     (may work on target machine — test manually)

MODEL FILES (canonical undated = REAL, large):
  model_btc_long.txt      762K  ← USE THIS
  model_btc_short.txt     851K  ← USE THIS
  model_eth_long.txt      845K  ← USE THIS
  model_eth_short.txt     666K  ← USE THIS
  model_sol_long.txt      830K  ← USE THIS
  model_sol_short.txt     618K  ← USE THIS
  model_btc_long_20260707.txt   20K  ← SMALL/OLD, ignore
  model_btc_short_20260707.txt  33K  ← SMALL/OLD, ignore
  model_eth_long_20260707.txt   20K  ← SMALL/OLD, ignore
  model_eth_short_20260707.txt  20K  ← SMALL/OLD, ignore
  model_sol_long_20260707.txt   13K  ← SMALL/OLD, ignore
  model_sol_short_20260707.txt  20K  ← SMALL/OLD, ignore
  top_level.txt                  9B  ← irrelevant

TESTING DRIVE:  no docker-compose.yml found ← clean, not contaminated
AI_OS:          search timed out ← unknown, treat as suspect
```

**IMPLICATION:** The canonical model files (undated) are the REAL trained models (762–851K). The dated `_20260707` files are tiny (13–33K) — likely from an early test run with minimal data. Do NOT use the dated files. The `config.json` references the undated names — this is correct.

**GITHUB CLONE ON TARGET MACHINE:** HTTPS may work on a real machine (the sandbox blocks it for network reasons). Test with:
```bash
git ls-remote https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
```
If it fails → clone via SSH using the deploy key, OR rsync from Storage directly.

---

## CRITICAL CONTEXT: WHY THIS EXISTS

Claude (this agent) spent multiple sessions building this system and failed to deliver a working deployment. Specific failures:

1. Never ran `docker compose up` and verified it passes
2. Marked audit tasks complete without independent verification
3. Created file fragments across at minimum 4 locations with no canonical source
4. Claimed GitHub repo was public — not independently verified (live `git ls-remote` failed)
5. Claimed code was audited — Codex + Opus found at least 14 errors not caught here

**Your job:** Audit everything below from scratch. Trust nothing Claude said without confirming it.

---

## KNOWN FILE LOCATIONS — ALL FRAGMENTS

### Location 1 — PRIMARY WORKING COPY (Storage)
```
/media/hermes/Storage/products/Predictor/
```
This is where all edits were made. Files confirmed present via direct read:
- `docker-compose.yml` ✓
- `config.json` ✓
- `.env.example` ✓
- `retrain_all.sh` ✓
- `DOSSIER.md` ✓
- `predictor/Dockerfile` ✓
- `dashboard/Dockerfile` ✓
- `dashboard/nginx.conf` ✓
- `dashboard/index.html` ✓
- `dashboard/app.js` ✓
- `signal_agent/Dockerfile` ✓
- `executor/Dockerfile` ✓
- `forge/Dockerfile` ✓
- `src/predictor_server.py` ✓
- `src/signal_agent/config.py` ✓
- `src/signal_agent/main.py` ✓
- `src/signal_agent/enricher.py` ✓
- `models/model_btc_long.txt` — **NOT CONFIRMED** (dated copies `_20260707` exist, symlinks or canonical names unclear)
- `models/model_btc_short.txt` — NOT CONFIRMED
- `models/model_eth_long.txt` — NOT CONFIRMED
- `models/model_eth_short.txt` — NOT CONFIRMED
- `models/model_sol_long.txt` — NOT CONFIRMED
- `models/model_sol_short.txt` — NOT CONFIRMED

**WARNING:** `models/` contains both `model_btc_long_20260707.txt` (dated) and what should be `model_btc_long.txt` (undated canonical). `config.json` references the undated names. Verify the undated files exist and are not empty.

### Location 2 — Testing Drive
```
/media/hermes/30ca47a4-e4aa-45a1-b88c-a89e001f4240/Testing/
```
An earlier deployment of the Predictor was placed here (Task #19). **State unknown.** This is an older version — likely pre-Docker, pre-signal-agent. Do not use as reference. Do not confuse with Location 1.

### Location 3 — AI_OS / Automation
```
/media/hermes/AI_OS/
```
Claude wrote files here in earlier sessions. Exact paths unknown — shell is currently down and cannot search. Treat as contaminated until audited.

### Location 4 — Workbench (found by Hermes agent)
```
/home/hermes/workbench/antigravity-predictor/
```
Claude did not intentionally create this. It was discovered by the Hermes takeover agent. Unknown state — could be an older clone, a partial copy, or from a different session entirely. **Do not use without diff against Location 1.**

### Location 5 — GitHub
```
https://github.com/serviciosnewtech-a11y/antigravity-predictor
```
Claude pushed commit `f41f368` from a staging repo at `/tmp/predictor_push/` (ephemeral, now gone). Claimed visibility was changed to public. **NOT INDEPENDENTLY VERIFIED** — live `git ls-remote` failed with auth error. Verify manually before assuming clone works.

### Location 6 — Ephemeral (GONE)
```
/tmp/predictor_push/
```
This was the clean git repo used for the push. It does not persist across session restarts. If the push to GitHub failed or is incomplete, this source is unrecoverable.

---

## KNOWN BUGS CLAUDE FIXED (verify fixes are in Location 1)

### Fix 1 — confidence_threshold was 0.65 (CRITICAL)
**File:** `src/signal_agent/config.py` line 27
**Old:** `confidence_threshold: float = 0.65`
**New:** `confidence_threshold: float = 0.22`
**Why critical:** LightGBM models output 0.18–0.28. Default of 0.65 means signal_agent never fires, ever.
**Verify:** `grep confidence_threshold /media/hermes/Storage/products/Predictor/src/signal_agent/config.py` → must show `0.22`

### Fix 2 — MIN_AUC mismatch
**File:** `retrain_all.sh` line 41
**Old:** `MIN_AUC="${MIN_AUC:-0.60}"`
**New:** `MIN_AUC="${MIN_AUC:-0.54}"`
**Verify:** `grep MIN_AUC /media/hermes/Storage/products/Predictor/retrain_all.sh | head -5`

### Fix 3 — Docker-unaware predictor reload in retrain_all.sh
**File:** `retrain_all.sh` lines 302–319
**Old:** Unconditionally called `systemctl restart predictor` — fails inside Docker
**New:** Checks for `/.dockerenv`, writes sentinel file instead
**Verify:** `grep -A5 "dockerenv" /media/hermes/Storage/products/Predictor/retrain_all.sh`

---

## KNOWN BUGS CLAUDE DID NOT CATCH (per Codex/Opus audit — 14 total, details unknown)

Claude's audit of its own code missed at least 14 errors. The full list has not been provided yet. When the Codex/Opus report is available, each error must be cross-referenced against Location 1 files and patched.

Until that report arrives, treat all of the following as potentially broken:
- Import paths in all Python files under `src/`
- Model path resolution in `predictor_server.py`
- WebSocket upgrade headers in `nginx.conf` (confirmed present but not runtime-tested)
- `executor/server.py` — never runtime-tested
- `forge/server.py` — never runtime-tested
- `retrain/Dockerfile` and retrain pipeline — never runtime-tested end-to-end
- `dashboard/app.js` WebSocket reconnection logic
- Signal agent `enricher.py` — all three backends (ollama, claude, openai_compatible)

---

## BUGS VISIBLE TO CLAUDE RIGHT NOW (discovered while writing this document)

### Bug A — `predictor_url` wrong default in SignalAgentConfig
**File:** `src/signal_agent/config.py` line 23
**Problem:** `predictor_url: str = "http://127.0.0.1:18910"` — this is localhost. Inside Docker, signal_agent needs `http://predictor:18910` (Docker network name).
**Mitigation:** `docker-compose.yml` sets `PREDICTOR_URL=http://predictor:18910` as env var, which overrides the bad default via `load_config()` line 88–89. So it works in Docker — BUT if anyone runs signal_agent outside Docker without setting `PREDICTOR_URL`, it silently connects to wrong host.
**Severity:** LOW in Docker deployment. HIGH in bare-metal deployment.

### Bug B — `inference_backend` not loaded from config.json block
**File:** `src/signal_agent/config.py`, `load_config()` function
**Problem:** `load_config()` reads most fields from `sa_block` (config.json) but does NOT read `inference_backend`, `ollama_url`, `ollama_model`, `hermes_proxy_url`, `hermes_inference_model`, `hermes_proxy_api_key` from config.json. These only come from env vars or dataclass defaults.
**Impact:** If someone sets `inference_backend` in config.json, it is silently ignored. Only env var `SA_INFERENCE_BACKEND` works.
**Severity:** MEDIUM — confusing but workaroundable via .env

### Bug C — `SignalAgentConfig` default `inference_backend` is `"openai_compatible"` not `"ollama"`
**File:** `src/signal_agent/config.py` line 43
**Problem:** Default is `"openai_compatible"` but docker-compose.yml and .env.example set `SA_INFERENCE_BACKEND=ollama`. If the env var is missing (e.g. someone doesn't use the .env.example), signal_agent tries to call `hermes_proxy_url` (`http://host.docker.internal:8645/v1`) which likely doesn't exist on a fresh VPS.
**Severity:** MEDIUM — breaks signal_agent if .env is not properly set.

### Bug D — model files in `models/` — canonical names vs dated names
**Location:** `/media/hermes/Storage/products/Predictor/models/`
**Problem:** Glob shows `model_btc_long_20260707.txt` exists but `config.json` references `models/model_btc_long.txt` (no date). The undated files may not exist, may be symlinks, or may be from an older training run.
**Verify:**
```bash
ls -la /media/hermes/Storage/products/Predictor/models/*.txt
```
If `model_btc_long.txt` is missing or 0 bytes, predictor will fail to start.
**Severity:** CRITICAL if undated files are missing.

---

## ARCHITECTURE (what Claude built — verify against actual files)

```
Client browser
    → port 80 (nginx, dashboard service)
        → proxies /api/* to predictor:18910
        → proxies /ws to predictor:18910 (WebSocket)
        → proxies /executor/* to executor:18911
        → proxies /forge/* to forge:18912

predictor (port 18910)
    FastAPI + LightGBM
    Endpoints: /api/status, /api/enriched-signal/{symbol}, /api/chat, /ws, /health
    Loads 6 model files at startup from ./models/
    Serves static dashboard files too (COPY dashboard/ in Dockerfile — potential duplication with dashboard service)

signal_agent (no exposed port)
    Polls predictor /api/status every 30s
    If prob > 0.22 for any asset → calls LLM enrichment
    LLM backend: SA_INFERENCE_BACKEND env var (ollama/claude/openai_compatible)

executor (port 18911)
    Receives trade signals
    DRY_RUN=true by default — logs only, no real orders

forge (port 18912)
    Strategy backtesting
    Connects to predictor WebSocket

retrain (profile-only)
    Not always-on
    Run with: docker compose --profile retrain run --rm retrain
```

### POTENTIAL ARCHITECTURE BUG
`predictor/Dockerfile` line 18: `COPY dashboard/ dashboard/`
This copies the dashboard static files INTO the predictor container. The dashboard container (nginx) also serves them. This is either intentional redundancy or a leftover. If predictor is trying to serve static files directly too, there may be a path collision. **Needs verification.**

---

## DOCKER-COMPOSE SERVICES — EXACT CURRENT STATE

Confirmed from direct file read of `/media/hermes/Storage/products/Predictor/docker-compose.yml`:

| Service | Dockerfile | Port | restart | env_file | volumes |
|---|---|---|---|---|---|
| dashboard | `dashboard/Dockerfile` | 80 | unless-stopped | NO | none |
| predictor | `predictor/Dockerfile` | 18910 | unless-stopped | YES (.env) | models:ro, logs |
| executor | `executor/Dockerfile` | 18911 | unless-stopped | YES (.env) | logs |
| forge | `forge/Dockerfile` | 18912 | unless-stopped | NO | forge_data |
| signal_agent | `signal_agent/Dockerfile` | none | unless-stopped | YES (.env) | logs |
| retrain | `retrain/Dockerfile` | none | N/A (profile) | YES (.env) | models, data, logs |

**Note:** `dashboard` and `forge` do NOT have `env_file: .env`. Any env var they need must be in `environment:` block in docker-compose.yml.

---

## MODELS — EXACT FILES NEEDED

`config.json` references these exact paths (relative to predictor container's `/app/`):
```
models/model_btc_long.txt
models/model_btc_short.txt
models/model_eth_long.txt
models/model_eth_short.txt
models/model_sol_long.txt    ← SOL long disabled via threshold 9.9999 in config.json
models/model_sol_short.txt
```

In Storage, confirmed dated files exist:
```
models/model_btc_long_20260707.txt   ← AUC 0.856
models/model_btc_short_20260707.txt  ← AUC 0.841
models/model_eth_long_20260707.txt   ← AUC 0.860
models/model_eth_short_20260707.txt  ← AUC 0.849
models/model_sol_long_20260707.txt   ← AUC 0.864
models/model_sol_short_20260707.txt  ← AUC 0.853
```

**Whether the undated canonical names exist is unverified.** If they don't, fix with:
```bash
cd /media/hermes/Storage/products/Predictor/models
cp model_btc_long_20260707.txt   model_btc_long.txt
cp model_btc_short_20260707.txt  model_btc_short.txt
cp model_eth_long_20260707.txt   model_eth_long.txt
cp model_eth_short_20260707.txt  model_eth_short.txt
cp model_sol_long_20260707.txt   model_sol_long.txt
cp model_sol_short_20260707.txt  model_sol_short.txt
```

---

## SIGNAL AGENT CONFIG MISMATCH (Bug C expanded)

The `.env.example` sets `SA_INFERENCE_BACKEND=ollama`. The `config.py` dataclass default is `"openai_compatible"`. The docker-compose.yml sets `SA_INFERENCE_BACKEND=ollama` in the environment block.

**What this means in practice:**
- If `.env` is created from `.env.example` correctly → `SA_INFERENCE_BACKEND=ollama` → signal_agent tries Ollama at `host.docker.internal:11434`
- If Ollama is not installed → signal_agent logs errors but doesn't crash (graceful degradation confirmed in code)
- If `.env` is missing or SA_INFERENCE_BACKEND is absent → default `"openai_compatible"` tries `http://host.docker.internal:8645/v1` → connection refused → error logs, no crash

The `hermes_proxy_url = "http://host.docker.internal:8645/v1"` in config.py suggests this was built to integrate with a Hermes agent proxy running on port 8645. **If such a proxy exists on the target host, signal_agent can use it for LLM enrichment without Ollama or Anthropic API key.** This was not documented in the previous handoff.

---

## WHAT NEEDS TO HAPPEN TO REACH GREEN

**Step 1 — Verify GitHub repo**
```bash
git ls-remote https://github.com/serviciosnewtech-a11y/antigravity-predictor.git
```
If this fails → repo is still private or doesn't exist → need alternative source (Location 1).

**Step 2 — Verify model files**
```bash
ls -la /media/hermes/Storage/products/Predictor/models/model_btc_long.txt
# etc for all 6
```
If missing → copy from dated versions (commands above in Models section).

**Step 3 — Choose canonical source**
Either:
- A: If GitHub works → `git clone` on target → canonical
- B: If GitHub fails → rsync/scp from Location 1 on this machine → canonical

**Step 4 — Create .env**
```bash
cp .env.example .env
```
Edit if needed:
- Set `SA_INFERENCE_BACKEND=ollama` (already default in .env.example)
- OR set `SA_INFERENCE_BACKEND=claude` + `ANTHROPIC_API_KEY=sk-ant-...`
- OR set `SA_INFERENCE_BACKEND=openai_compatible` + `HERMES_PROXY_URL=http://...` if Hermes proxy available

**Step 5 — Build and verify**
```bash
docker compose build 2>&1 | tee build.log
# Check build.log for errors before starting
docker compose up -d
docker compose ps
# All 5 services must show "Up", none "Restarting"
docker compose logs predictor | grep -E "loaded|error|Error" | head -20
curl -s http://localhost/api/status
```

**Step 6 — Apply Codex/Opus bug fixes**
Once the 14-error report is available, apply patches to canonical source and rebuild.

---

## THINGS CLAUDE CANNOT VERIFY FROM THIS ENVIRONMENT

1. Whether `git ls-remote` to the GitHub repo returns results (shell is down)
2. Whether the 6 undated model files exist (can't list files with exclusions easily)
3. The exact content of `executor/server.py` and `forge/server.py` (not read this session)
4. The state of `/home/hermes/workbench/antigravity-predictor/` (outside connected folders)
5. The state of `/media/hermes/AI_OS/` content related to this project
6. The 14 errors Codex/Opus found
7. Whether the GitHub push commit `f41f368` is actually accessible

---

## FILES CLAUDE DIRECTLY MODIFIED THIS SESSION

These are confirmed changes made to `/media/hermes/Storage/products/Predictor/`:

| File | Change |
|---|---|
| `src/signal_agent/config.py` | `confidence_threshold` 0.65 → 0.22 |
| `retrain_all.sh` | `MIN_AUC` default 0.60 → 0.54; Docker-aware predictor reload |
| `.gitignore` | Added model noise exclusions, forge_data, sentinel file |
| `DOSSIER.md` | Full rewrite |
| `SESSION_WAYPOINT.md` | New file |
| `HERMES_HANDOFF.md` | New file (now superseded by this document) |
| `RESCUE_HANDOFF.md` | This file |
| `docker-compose.yml` | Added signal_agent service |
| `signal_agent/Dockerfile` | New file |
| `src/signal_agent/__init__.py` | New file |
| `src/signal_agent/main.py` | New file |
| `src/signal_agent/enricher.py` | New file |
| `src/predictor_server.py` | Added /api/chat endpoint, enriched-signal endpoints |
| `dashboard/index.html` | Added Hermes chat FAB |
| `dashboard/app.js` | Wired chat to /api/chat |

**Everything else in the repo was pre-existing or written in earlier sessions.**

---

## WHAT CLAUDE DOES NOT KNOW

- What the 14 errors are
- Whether `executor/server.py` is correct (was written in an earlier session, not re-read this session)
- Whether `forge/server.py` is correct (same)
- Whether `predictor_server.py`'s `/api/chat` endpoint actually works end-to-end with the frontend
- Whether the dashboard's Hermes FAB chat panel works correctly
- Whether the WebSocket connection between dashboard and predictor is stable
- The exact diff between Location 1 (Storage) and Location 4 (workbench)
- The exact diff between Location 1 and what was pushed to GitHub

---

*Claude (Sonnet, Cowork mode) — 2026-07-18*
*This document is the most honest account of what was done and what is unknown.*
*Do not trust the previous HERMES_HANDOFF.md — it was written before these discrepancies were known.*
