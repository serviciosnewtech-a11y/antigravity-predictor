FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    lightgbm \
    ccxt \
    pandas \
    numpy \
    scikit-learn \
    pyarrow \
    yfinance \
    loguru \
    requests

# Install lgbm_poc cleanly (no conflicting top-level files)
COPY src/lgbm_poc /tmp/lgbm_poc_pkg/lgbm_poc
COPY predictor/lgbm_poc_setup.py /tmp/lgbm_poc_pkg/setup.py
RUN pip install --no-cache-dir /tmp/lgbm_poc_pkg

# Copy retrain source files
COPY src/_bootstrap.py            src/
COPY src/fetch_macro.py           src/
COPY src/prepare_full_dataset.py  src/
COPY src/train_lightgbm.py        src/
COPY src/download_ohlcv.py        src/
COPY src/summarize_run.py         src/
COPY config.json                  src/config.json
COPY retrain_all.sh               retrain_all.sh

RUN mkdir -p models data logs && chmod +x retrain_all.sh

ENV MIN_AUC=0.54
ENV PYTHONPATH=/app/src

CMD ["bash", "retrain_all.sh"]
