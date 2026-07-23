# Docker pre-test checklist

This exists because the bare-metal product just went through three rounds of
"looked fine, broke on the real machine anyway" in one session — a stale
`admin_agent` packaging gap, two rounds of cwd/path-resolution bugs (deploy
script cwd, then `config.json`/`dashboard/`/model-path resolution inside
`predictor.service`) — and the docker product has a real, sandbox-confirmed
version of the same *class* of bug (see "Already found and fixed" below).
Docker hasn't been tested on real hardware since beta-1.4's structural split.
The point of this file is to run the whole list in one pass before the next
live test, instead of discovering these one crash-loop at a time the way
bare-metal did.

**No Docker daemon is available in the sandbox this was written in.** Every
item below was verified as far as static analysis + a manual re-creation of
each Dockerfile's exact `COPY` set + actually exec'ing the resulting
`predictor_server.py` module can go, without an actual `docker build`. That
is a real gap — a real `docker compose build && up` can still surface things
this couldn't (base image issues, layer caching weirdness, actual inter-
container networking, volume permission mismatches, `libgomp1`/lightgbm ABI
issues, etc.). Treat everything here as "passed the checks that don't
require a daemon," not "fully verified."

## Already found and fixed (this session, before any live test)

- **`predictor.Dockerfile` was missing `COPY src/signal_log.py`.** Added in
  beta-1.8 (durable signal/trade history), `signal_log.py` is imported
  unconditionally at the top of `predictor_server.py`
  (`import signal_log`), but the Dockerfile's `COPY` list was never updated
  to match. Every build from beta-1.8 onward would have hit
  `ModuleNotFoundError: No module named 'signal_log'` at container startup —
  never caught because `pytest` runs against the full repo checkout, not the
  built image. Fixed by adding the `COPY` line and verified by manually
  reproducing the Dockerfile's exact file set in an isolated directory and
  actually exec'ing `predictor_server.py`'s full module body (config load,
  dashboard mount, `signal_log` import) against it — succeeded.

## The general lesson (read this before touching any Dockerfile)

Every relative path in `predictor_server.py` (`config.json` fallback,
`dashboard/` mount, `GOLD_PARQUET_PATH` default, model paths via
`resolve_model_path()`) resolves against either `WORKDIR`/cwd or this file's
own `__file__` location — never assume a path "just works" because it did in
one deployment mode. Bare-metal's `predictor.service` sets
`WorkingDirectory=<APP_DIR>/src`; Docker's `WORKDIR /app` + `CMD ["python",
"src/predictor_server.py"]` means cwd is `/app`. These are *different*, and
code that happens to work in one can silently break in the other. Confirmed
already-correct for Docker specifically:

- `config.json` → baked in via `COPY config.json src/config.json`, read
  through `predictor_server.py`'s `__file__`-relative fallback — correct.
- `dashboard/` → baked in via `COPY dashboard/ dashboard/`, resolves via the
  same `__file__`-relative fallback — correct.
- `GOLD_PARQUET_PATH` default (`data/macro/gold.parquet`) → resolves against
  cwd=`/app`, and `data/macro/` is both baked in at build time and bind-
  mounted at `/app/data` — correct.
- `resolve_model_path()` (added beta-1.10, fixing the bare-metal model-path
  bug) → `MODELS_DIR` is not set as a container env var in
  `docker-compose.yml` (it's only used to pick the *host-side* bind-mount
  source), so the function falls back to its `__file__`-relative default,
  which computes `/app/src/../models` = `/app/models` — matches the
  `${MODELS_DIR:-../../models}:/app/models:ro` bind mount. Correct.

**Whenever a new top-level `import some_module` is added to
`predictor_server.py`, grep this checklist's rule back into memory: does
`predictor.Dockerfile` have a matching `COPY src/some_module.py
src/some_module.py`?** This is the exact bug that was just found and fixed.
The same question applies to `executor/server.py`, `forge/*.py`, and
`signal_agent/*.py` against their own Dockerfiles.

## New gap found 2026-07-23, not yet fixed for docker

A real bare-metal deploy of beta-1.10.3 turned up two real chat-backend
plumbing gaps, both fixed for bare-metal in beta-1.10.4:

1. `predictor.service` never had `EnvironmentFile=-/opt/predictor/.env` at
   all (unlike `signal_agent.service`), so nothing in `.env` — including any
   chat backend config — ever reached the process serving `/api/chat`.
   **Check whether `docker-compose.yml` has the equivalent gap** — i.e.
   confirm the `predictor` service's `env_file:`/`environment:` actually
   passes `HERMES_PROXY_URL`/`CHAT_BACKEND`/`ANTHROPIC_API_KEY`/etc. through
   to the container, not just `MODELS_DIR`/`DATA_DIR`-style path vars.
2. `tools/agent_chat_relay.py` (the local, no-API-key CLI-agent chat
   backend — Pattern B, the actual default per `.env.example`) is wired
   into `run_monolith.sh` and now `deploy/bare-metal/agent_relay.service`,
   but **has no docker-compose service at all**. If the docker product
   should also ship with a working local-agent chat backend by default
   (matching bare-metal), it needs its own service block in
   `docker-compose.yml` (build from a thin Dockerfile or reuse the
   predictor image, `ENABLE_AGENT_RELAY`-equivalent env, expose 8645 only
   on the internal compose network, and `HERMES_PROXY_URL=http://agent_relay:8645`
   in the predictor service's environment) before this can be considered
   at parity with bare-metal. Not done yet — flagging so it isn't
   rediscovered from scratch when docker's live test finally happens.

## Checklist for the next live docker test

Run in order — each step is cheap and catches a different failure class:

1. **Diff every Dockerfile's `COPY` list against the actual `import`
   statements of the file(s) it copies.** Do this by hand, not from memory —
   this is what was missed for `signal_log.py`. Repeat for all six
   Dockerfiles (`predictor`, `executor`, `forge`, `signal_agent`,
   `dashboard`, `retrain`), not just predictor.
2. **`docker compose config`** from `deploy/docker/` — resolves every `${VAR}`
   substitution and confirms `.env` parses. Cheapest possible check, catches
   YAML/env typos before a slow build.
3. **`docker compose build`** for all services. Watch for `COPY` failures
   (missing source file — the class of bug just fixed) and pip install
   failures.
4. **`docker compose up`**, then for each service actually check it's alive,
   not just "container running":
   - `curl http://localhost/api/status` (through nginx/dashboard) and
     `docker compose exec predictor curl http://localhost:18910/api/status`
     (direct, bypassing nginx — isolates proxy config bugs from app bugs).
   - `docker compose logs predictor --tail 50` — look for the `signal_log`
     init, all three `AssetEngine`s reporting "Models loaded", and no
     tracebacks.
   - Confirm `/app/logs/signal_history.db` gets created inside the
     `predictor` container (`docker compose exec predictor ls -la logs/`) —
     this is the beta-1.8 feature; if the bind-mounted `logs/` volume has
     the wrong host-side permissions, SQLite can fail to create the file
     silently-ish (a logged error, not a crash) — check for that error
     specifically in the predictor log.
   - Open the dashboard in an actual browser (or `curl -s http://localhost/
     | head`) — confirms nginx→dashboard container→predictor proxy chain
     end to end, not just that each container individually started.
5. **Only after all of the above passes clean**, consider the docker product
   as verified as the bare-metal one now is (i.e., beta-1.10-equivalent
   confidence) and safe to hand off for a real deploy.

## Why this file exists instead of just being chat history

Chat context resets between sessions; this file doesn't. If a future session
picks up docker testing cold, read this file first — it's cheaper than
re-discovering the `signal_log.py` gap (or whatever the next one is) through
a live crash-loop.
