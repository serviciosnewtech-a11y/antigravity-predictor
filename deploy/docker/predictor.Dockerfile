FROM python:3.11-slim

WORKDIR /app

# System deps for LightGBM (libgomp for OpenMP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (unpinned — latest compatible)
COPY deploy/docker/predictor-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# App source — predictor_server.py imports only stdlib + the deps above,
# plus feature_gate.py (H-13 P1 fail-loud parity gate), signal_log.py
# (durable SQLite signal/trade history), and llm_backend.py + hermes_persona.py
# (shared Hermes brain — backend resolution + identity/memory, also used by
# signal_agent/enricher.py; requests/loguru only, no extra pip deps beyond
# what's already installed above). Every top-level `import X` in
# predictor_server.py MUST have a matching COPY line here, or the container
# crash-loops on ModuleNotFoundError at startup — signal_log.py itself was
# missing here for a while after it was added (never caught because pytest
# runs against the full repo checkout, not the built image; only a real
# `docker compose build && up` catches this class of bug). It does NOT
# import lgbm_poc or signal_agent, so neither is copied/installed.
COPY src/predictor_server.py  src/predictor_server.py
COPY src/feature_gate.py      src/feature_gate.py
COPY src/signal_log.py        src/signal_log.py
COPY src/llm_backend.py       src/llm_backend.py
COPY src/hermes_persona.py    src/hermes_persona.py
COPY config.json              src/config.json
COPY dashboard/               dashboard/

# Bake in the macro parquet cache (gold/oil/dxy/spx/vix) as a fallback so
# /api/market-tickers has real gold data even before the docker-compose
# bind-mount (${DATA_DIR:-./data}:/app/data) is populated, or on a network
# where live Yahoo Finance fetches are blocked. predictor_server.py only
# ever reads this cache directly — it never calls yfinance itself, so no
# extra Python dependency is needed here. The bind-mount (if present)
# still takes precedence at runtime and can supply fresher data.
COPY data/macro/               data/macro/

# Models + logs are provided at runtime via bind mounts
RUN mkdir -p models logs

EXPOSE 18910

CMD ["python", "src/predictor_server.py"]
