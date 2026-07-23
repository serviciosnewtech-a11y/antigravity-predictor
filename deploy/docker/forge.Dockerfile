FROM python:3.11-slim

WORKDIR /app

# Unpinned deps — forge imports fastapi, uvicorn, websockets, loguru, pydantic.
# (sqlite3, statistics, uuid, dataclasses, asyncio are stdlib.)
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    websockets \
    loguru \
    pydantic

# Copy forge package (run as a module: python -m forge.server)
COPY forge/__init__.py   forge/__init__.py
COPY forge/server.py     forge/server.py
COPY forge/collector.py  forge/collector.py
COPY forge/simulator.py  forge/simulator.py
COPY forge/strategies.py forge/strategies.py
COPY forge/db.py         forge/db.py

RUN mkdir -p forge_data

EXPOSE 18912

ENV FORGE_PORT=18912
ENV PREDICTOR_WS_URL=ws://predictor:18910/ws
ENV ATR_PERIOD=14

CMD ["python", "-m", "forge.server"]
