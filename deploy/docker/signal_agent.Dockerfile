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

# Copy signal_agent package + shared config + the shared Hermes brain
# (llm_backend.py, hermes_persona.py — backend resolution + identity/memory,
# same modules predictor.Dockerfile copies for /api/chat; stdlib + requests/
# loguru only, both already installed above). enricher.py/main.py import
# these via `import llm_backend` / `import hermes_persona` after adding
# src/ to sys.path themselves — PYTHONPATH below also covers it, but the
# explicit COPY here is still required regardless of PYTHONPATH, same rule
# as predictor.Dockerfile: every top-level import needs a matching COPY.
COPY src/signal_agent/     src/signal_agent/
COPY src/llm_backend.py    src/llm_backend.py
COPY src/hermes_persona.py src/hermes_persona.py
COPY config.json           src/config.json

RUN mkdir -p logs

# config.py resolves _CONFIG_PATH = Path(__file__).parent.parent / "config.json"
# = /app/src/config.json  ✓
ENV PYTHONPATH=/app/src

# Safety default — overridden by .env via docker-compose env_file.
# NEVER restore to 0.65: model outputs are 0.18-0.28.
ENV SA_CONFIDENCE_THRESHOLD=0.22

CMD ["python", "-m", "signal_agent.main"]
