FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    ccxt \
    loguru \
    requests \
    pydantic

COPY executor/server.py src/executor_server.py

RUN mkdir -p logs

EXPOSE 18911

CMD ["python", "src/executor_server.py"]
