FROM python:3.11-slim

WORKDIR /app

# System deps for LightGBM (libgomp for OpenMP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (unpinned — latest compatible)
COPY predictor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# App source — predictor_server.py imports only stdlib + the deps above.
# It does NOT import lgbm_poc or signal_agent, so neither is copied/installed.
COPY src/predictor_server.py  src/predictor_server.py
COPY config.json              src/config.json
COPY dashboard/               dashboard/

# Models + logs are provided at runtime via bind mounts
RUN mkdir -p models logs

EXPOSE 18910

CMD ["python", "src/predictor_server.py"]
