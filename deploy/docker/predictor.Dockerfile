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
# plus feature_gate.py (H-13 P1 fail-loud parity gate). It does NOT import
# lgbm_poc or signal_agent, so neither is copied/installed.
COPY src/predictor_server.py  src/predictor_server.py
COPY src/feature_gate.py      src/feature_gate.py
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
