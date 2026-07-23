FROM python:3.11-slim

WORKDIR /app

# signal_agent deps:
#   anthropic          — Claude API synthesis (SA_INFERENCE_BACKEND=claude)
#   duckduckgo-search  — news fetch, no API key required
#   requests           — predictor REST calls
#   loguru             — structured logging
# anthropic + ddgs degrade gracefully if unavailable at runtime.
RUN pip install --no-cache-dir \
    anthropic \
    duckduckgo-search \
    requests \
    loguru

# Copy signal_agent package + shared config
COPY src/signal_agent/ src/signal_agent/
COPY config.json       src/config.json

RUN mkdir -p logs

# config.py resolves _CONFIG_PATH = Path(__file__).parent.parent / "config.json"
# = /app/src/config.json  ✓
ENV PYTHONPATH=/app/src

# Safety default — overridden by .env via docker-compose env_file.
# NEVER restore to 0.65: model outputs are 0.18-0.28.
ENV SA_CONFIDENCE_THRESHOLD=0.22

CMD ["python", "-m", "signal_agent.main"]
