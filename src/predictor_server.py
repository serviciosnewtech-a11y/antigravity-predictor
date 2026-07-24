"""
Antigravity Predictor Server — v2  (BTC / ETH / SOL multi-asset)
"""
import os, json, asyncio, threading, time
import xml.etree.ElementTree as ET
import requests
import numpy as np
import pandas as pd
import lightgbm as lgb
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from fastapi import Depends, FastAPI, Header, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger
import websockets
from typing import Optional, List

from feature_gate import evaluate_feature_parity, format_gate_log_summary
import signal_log
import llm_backend
import hermes_persona

# ── Config ───────────────────────────────────────────────────────────────────
# src/config.json is normally created by run_monolith.sh/run_local.sh (which
# sync the repo-root config.json into src/ before launch) or baked in by
# deploy/docker/predictor.Dockerfile's `COPY config.json src/config.json`.
# Neither of those run before a bare `pytest` invocation from a clean
# checkout, so fall back to the repo-root copy directly rather than
# requiring every caller to remember the sync step first.
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_SRC_DIR, "config.json")
if not os.path.exists(CONFIG_PATH):
    CONFIG_PATH = os.path.join(os.path.dirname(_SRC_DIR), "config.json")
try:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
except Exception as e:
    logger.error(f"Failed to load config: {e}")
    raise

ASSETS = list(config["assets"].keys())          # ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
DISPLAY_ASSETS = ASSETS + ["XAU/USD"]
MACRO_DISPLAY_ASSETS = {"XAU/USD"}
# Relative default: resolves to /app/data/macro/gold.parquet in Docker
# (WORKDIR /app, same as before) and to <repo-root>/data/macro/gold.parquet
# bare-metal. Same fix as forge/db.py's FORGE_DATA_DIR — an absolute
# "/app/..." default only worked by coincidence inside a container.
GOLD_PARQUET_PATH = os.environ.get("GOLD_PARQUET_PATH", "data/macro/gold.parquet")

# config.json's model_long_path/model_short_path (e.g. "models/model_btc_long.txt")
# are repo-root-relative strings, historically opened raw against cwd. That only
# ever worked by coincidence: Docker's predictor container has WORKDIR=/app (the
# repo root), so it happened to line up, but bare-metal's predictor.service sets
# WorkingDirectory=<APP_DIR>/src (so local imports resolve without a package
# install step), which makes the same raw path look for src/models/... instead
# of the real <APP_DIR>/models/... one level up — found via a real fresh
# bare-metal install crash-looping (exit status 3) rather than in this sandbox.
# Resolve via this file's own location instead, which is correct regardless of
# cwd — same fix already applied to signal_log.py's LOGS_DIR and the models_dir
# lookup in _model_performance_context() below.
_MODELS_DIR = os.environ.get("MODELS_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "models"
)


def resolve_model_path(cfg_path: str) -> str:
    """Resolve a config.json model_*_path value to an actual file, independent
    of the process's current working directory."""
    candidate = os.path.join(_MODELS_DIR, os.path.basename(cfg_path))
    if os.path.exists(candidate):
        return candidate
    # Fall back to the raw cfg value as-given (e.g. an absolute path, or a
    # cwd-relative path that happens to be correct for the current caller).
    return cfg_path
TIMEFRAME = config.get("timeframe", "15m")
SUPPORTED_TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": "D"}
TF_MINS = SUPPORTED_TIMEFRAMES.get(TIMEFRAME, 15)

def normalise_timeframe(tf: str | None) -> str:
    tf = (tf or TIMEFRAME).strip().lower()
    aliases = {"1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m", "60m": "1h", "240m": "4h", "d": "1d", "day": "1d"}
    tf = aliases.get(tf, tf)
    if tf not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(status_code=400, detail=f"Unsupported timeframe: {tf}")
    return tf

def bybit_interval(tf: str):
    return SUPPORTED_TIMEFRAMES[normalise_timeframe(tf)]

def is_macro_display_asset(symbol: str | None) -> bool:
    return (symbol or "").upper() in MACRO_DISPLAY_ASSETS

def _required_col(df: pd.DataFrame, *names: str) -> str:
    lookup = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    raise HTTPException(status_code=503, detail=f"Gold macro feed missing column: {names[0]}")

# In-memory cache for the gold parquet: this is daily OHLCV (at most one new
# row per day), but was previously being re-read from disk and re-parsed on
# every single call — and this function is called from /api/market-tickers,
# which the dashboard hits on every websocket tick (multiple times a
# second across 3 assets). A 5-minute cache eliminates that redundant I/O
# without ever serving data staler than the source file itself updates.
_GOLD_CACHE: dict = {"rows": None, "loaded_at": 0.0}
_GOLD_CACHE_TTL_S = 300.0

# Rate-limits the "unavailable" warning so a missing/broken feed logs once
# per window instead of once per tick (previously: effectively every
# websocket update, i.e. a warning-log flood). The feed being down is worth
# knowing about; the same warning dozens of times a minute is not.
_GOLD_WARN_STATE: dict = {"last_warned_at": 0.0, "suppressed_count": 0}
_GOLD_WARN_INTERVAL_S = 300.0


def fetch_gold_daily_candles(limit: int = 300) -> list[dict]:
    """Return real daily Gold candles from the mounted macro dataset (cached, see _GOLD_CACHE above)."""
    now = time.time()
    if _GOLD_CACHE["rows"] is not None and (now - _GOLD_CACHE["loaded_at"]) < _GOLD_CACHE_TTL_S:
        return _GOLD_CACHE["rows"][-limit:]

    if not os.path.exists(GOLD_PARQUET_PATH):
        raise HTTPException(status_code=503, detail="Gold macro feed unavailable")
    df = pd.read_parquet(GOLD_PARQUET_PATH).sort_index()
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True)
    elif "date" in df.columns:
        ts = pd.to_datetime(df["date"], utc=True)
    else:
        ts = pd.Series(pd.to_datetime(df.index, utc=True), index=df.index)
    open_col = _required_col(df, "open", "Open")
    high_col = _required_col(df, "high", "High")
    low_col = _required_col(df, "low", "Low")
    close_col = _required_col(df, "close", "Close", "adj_close", "Adj Close")
    volume_col = None
    try:
        volume_col = _required_col(df, "volume", "Volume")
    except HTTPException:
        volume_col = None
    rows = []
    for idx in range(len(df)):
        row = df.iloc[idx]
        try:
            rows.append({
                "time": int(ts.iloc[idx].timestamp()),
                "open": float(row[open_col]),
                "high": float(row[high_col]),
                "low": float(row[low_col]),
                "close": float(row[close_col]),
                "volume": float(row[volume_col]) if volume_col else 0.0,
                "source": "macro_gold_parquet",
            })
        except Exception:
            continue
    _GOLD_CACHE["rows"] = rows
    _GOLD_CACHE["loaded_at"] = now
    return rows[-max(1, min(limit, 1000)):]

# ── WebSocket Connection Manager ─────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws) if hasattr(self.active, "discard") else None
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# ── Feature expansion (H-13 follow-up: funding/mark/basis + cross-asset) ────
# The original H-13 retrain deliberately shipped only the 41 features
# computable from a single asset's own OHLCV buffer, to close the signal
# gap fast. These two families are the next-cheapest to add for real:
#   - primary_futures_funding_mark_basis (8 cols): Bybit's public tickers
#     endpoint already returns fundingRate/markPrice/indexPrice for free,
#     no auth, one call per asset.
#   - cross_asset_peers (8 cols): the other two AssetEngine instances
#     already run in-process computing their own basic features every
#     tick — reading their last computed row costs nothing extra.
# Column names for the peer family are dynamic per-model: whichever two
# of {btc,eth,sol} are NOT this model's own asset, in canonical ASSETS
# order. E.g. the BTC model gets eth_return_1/sol_return_1/etc; the ETH
# model gets btc_return_1/sol_return_1/etc.
FUNDING_FEATURE_COLUMNS = [
    "funding_rate", "funding_rate_abs", "funding_rate_mean_4", "funding_rate_std_4",
    "mark_basis", "mark_premium", "mark_premium_mean_4", "futures_pressure",
]
PEER_FEATURE_SUFFIXES = ["return_1", "return_3", "trend", "volume_block"]

# ── Higher-timeframe BTC context (feature-expansion round 2) ────────────────
# Council consensus (independent Opus + Fable review) on going from 57
# toward ~100 features: feature count was not the bottleneck at 41->57
# (AUC flat), so most of the remaining 126-feature schema — full 1h/4h/1d
# higher-timeframe (27 cols), microstructure (12 cols), macro (30 cols) —
# was NOT worth building given ~19k rows/asset and a 7-12% positive rate.
# The one family both reviewers endorsed: a TRIMMED higher-timeframe BTC
# tier (regime/trend context genuinely orthogonal to 15m features), skip
# the 1d tier (too few daily bars to be meaningful), skip macro (daily data
# forward-filled onto 15m candles is near-constant and reintroduces the
# same flaky-third-party-feed risk that caused H-13 for gold), skip
# microstructure (defer — more train/serve drift surface for a
# 1-hour-horizon label that 1-minute noise mostly can't inform).
#
# This family is BTC-specific and asset-agnostic — ALL THREE models (BTC/
# ETH/SOL) get the same BTC 1h/4h regime context, matching how the
# original 126-feature schema always named these "btc_1h_"/"btc_4h_"
# regardless of which asset the model was for (BTC dominance as shared
# market regime, not per-asset).
HTF_TIMEFRAMES = [("1h", 60), ("4h", 240)]   # (name, Bybit interval code)
HTF_FEATURE_SUFFIXES = ["trend_direction", "trend_strength", "return_3", "atr_percentile"]
HTF_FEATURE_COLUMNS = [f"btc_{tf}_{suf}" for tf, _ in HTF_TIMEFRAMES for suf in HTF_FEATURE_SUFFIXES]
assert len(HTF_FEATURE_COLUMNS) == 8

_BTC_HTF_CACHE: dict = {"data": None, "fetched_at": 0.0}
_BTC_HTF_CACHE_TTL_S = 300.0  # 1h/4h regime doesn't change fast; 5 min is plenty fresh


def fetch_btc_htf_context() -> Optional[dict]:
    """
    Live BTC 1h/4h trend/volatility context via Bybit's public kline
    endpoint. Shared across all 3 AssetEngine instances (module-level
    cache, not per-engine) since it's the same BTC-market-regime signal for
    every model. Returns None (not a fake zero) on any failure — callers
    must let build_features() omit these columns, which the H-13 gate
    correctly reports as missing rather than silently zero-filling.
    """
    now = time.time()
    if _BTC_HTF_CACHE["data"] is not None and (now - _BTC_HTF_CACHE["fetched_at"]) < _BTC_HTF_CACHE_TTL_S:
        return _BTC_HTF_CACHE["data"]
    try:
        ctx = {}
        for tf_name, interval in HTF_TIMEFRAMES:
            r = requests.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "linear", "symbol": "BTCUSDT", "interval": str(interval), "limit": 100},
                timeout=10,
            )
            rows = r.json().get("result", {}).get("list", [])
            if not rows:
                return None
            rows = list(reversed(rows))  # Bybit returns newest-first
            closes = pd.Series([float(row[4]) for row in rows])
            highs  = pd.Series([float(row[2]) for row in rows])
            lows   = pd.Series([float(row[3]) for row in rows])

            ema_fast = closes.ewm(span=9, adjust=False).mean()
            ema_slow = closes.ewm(span=21, adjust=False).mean()
            close_val = closes.replace(0, pd.NA)
            trend_strength  = ((ema_fast - ema_slow) / close_val).fillna(0.0)
            trend_direction = (ema_fast > ema_slow).astype(int) - (ema_fast < ema_slow).astype(int)
            return_3 = np.log(closes / closes.shift(3).replace(0, pd.NA)).fillna(0.0)
            atr_proxy = (highs - lows).rolling(14, min_periods=5).mean().fillna(0.0)
            atr_min = atr_proxy.rolling(50, min_periods=10).min()
            atr_max = atr_proxy.rolling(50, min_periods=10).max()
            atr_percentile = ((atr_proxy - atr_min) / (atr_max - atr_min).replace(0, pd.NA)).fillna(0.5)

            ctx[f"btc_{tf_name}_trend_direction"] = float(trend_direction.iloc[-1])
            ctx[f"btc_{tf_name}_trend_strength"]  = float(trend_strength.iloc[-1])
            ctx[f"btc_{tf_name}_return_3"]        = float(return_3.iloc[-1])
            ctx[f"btc_{tf_name}_atr_percentile"]  = float(atr_percentile.iloc[-1])

        _BTC_HTF_CACHE["data"] = ctx
        _BTC_HTF_CACHE["fetched_at"] = now
        return ctx
    except Exception as e:
        logger.warning(f"[htf] fetch_btc_htf_context failed: {e}")
        return None

_FUNDING_SNAPSHOT_CACHE: dict = {}   # symbol -> {"data": {...}, "fetched_at": ts}
_FUNDING_SNAPSHOT_TTL_S = 60.0


def fetch_funding_snapshot(symbol: str) -> Optional[dict]:
    """
    Live funding_rate/markPrice/indexPrice for one symbol via Bybit's public
    tickers endpoint (no auth required). Cached per-symbol for
    _FUNDING_SNAPSHOT_TTL_S to avoid hammering the endpoint on every tick.
    Returns None (not a fake zero) if the fetch fails — callers must treat
    that as "funding context unavailable" and let the H-13 gate see missing
    columns, not silently zero-fill.
    """
    now = time.time()
    cached = _FUNDING_SNAPSHOT_CACHE.get(symbol)
    if cached and (now - cached["fetched_at"]) < _FUNDING_SNAPSHOT_TTL_S:
        return cached["data"]
    try:
        bybit_sym = symbol.replace("/", "")
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": bybit_sym},
            timeout=5,
        )
        data = r.json()
        row = (data.get("result", {}).get("list") or [None])[0]
        if not row:
            return None
        snapshot = {
            "funding_rate": float(row["fundingRate"]),
            "mark_price": float(row["markPrice"]),
            "index_price": float(row["indexPrice"]),
        }
        _FUNDING_SNAPSHOT_CACHE[symbol] = {"data": snapshot, "fetched_at": now}
        return snapshot
    except Exception as e:
        logger.warning(f"[funding] fetch_funding_snapshot({symbol}) failed: {e}")
        return None


# ── Feature Engineering (shared across all assets) ───────────────────────────
def build_features(candles: list[dict], funding_ctx: Optional[dict] = None, peer_ctx: Optional[dict] = None,
                    htf_ctx: Optional[dict] = None) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["time"], unit="s")
    out = df.sort_values("date").reset_index(drop=True)

    close_val = out["close"].replace(0, pd.NA)
    out["log_return_1"] = np.log(out["close"] / out["close"].shift(1).replace(0, pd.NA)).fillna(0.0)
    out["log_return_3"] = np.log(out["close"] / out["close"].shift(3).replace(0, pd.NA)).fillna(0.0)
    out["log_return_6"] = np.log(out["close"] / out["close"].shift(6).replace(0, pd.NA)).fillna(0.0)

    range_raw = out["high"] - out["low"]
    out["range_1"]  = (range_raw / close_val).fillna(0.0)
    out["body"]     = (out["close"] - out["open"]).abs()
    out["atr_proxy"] = range_raw.rolling(14, min_periods=5).mean().fillna(0.0)
    out["volatility_lookback"] = out["log_return_1"].rolling(20, min_periods=5).std().fillna(0.0)

    out["hour_of_day"]   = out["date"].dt.hour.fillna(0).astype(int)
    out["day_of_week"]   = out["date"].dt.dayofweek.fillna(0).astype(int)
    out["session_asia"]    = out["hour_of_day"].between(0, 7).astype(int)
    out["session_london"]  = out["hour_of_day"].between(7, 13).astype(int)
    out["session_newyork"] = out["hour_of_day"].between(13, 21).astype(int)

    out["vol_mean_20"] = out["volume"].rolling(20, min_periods=5).mean().fillna(1.0)
    out["vol_std_20"]  = out["volume"].rolling(20, min_periods=5).std().fillna(0.0)
    out["volume_zscore"]    = ((out["volume"] - out["vol_mean_20"]) / out["vol_std_20"].replace(0, pd.NA)).fillna(0.0)
    out["relative_volume"]  = (out["volume"] / out["vol_mean_20"]).fillna(1.0)
    vol_min = out["volume"].rolling(100, min_periods=10).min()
    vol_max = out["volume"].rolling(100, min_periods=10).max()
    out["volume_percentile"] = ((out["volume"] - vol_min) / (vol_max - vol_min).replace(0, pd.NA)).fillna(0.5)

    range_safe = range_raw.replace(0, pd.NA)
    out["body_ratio"]       = (out["body"] / range_safe).fillna(0.0)
    close_open_max = out[["close", "open"]].max(axis=1)
    close_open_min = out[["close", "open"]].min(axis=1)
    out["upper_wick_ratio"] = ((out["high"] - close_open_max) / range_safe).fillna(0.0)
    out["lower_wick_ratio"] = ((close_open_min - out["low"]) / range_safe).fillna(0.0)
    out["atr_normalized_range"] = (range_raw / out["atr_proxy"].replace(0, pd.NA)).fillna(0.0)
    out["stop_distance"]    = (out["atr_proxy"] / close_val).fillna(0.0)

    out["ema_fast"]       = out["close"].ewm(span=9, adjust=False).mean()
    out["ema_slow"]       = out["close"].ewm(span=21, adjust=False).mean()
    out["dist_ema_fast"]  = ((out["close"] - out["ema_fast"]) / close_val).fillna(0.0)
    out["dist_ema_slow"]  = ((out["close"] - out["ema_slow"]) / close_val).fillna(0.0)
    out["trend_strength"] = ((out["ema_fast"] - out["ema_slow"]) / close_val).fillna(0.0)
    out["trend_direction"] = (out["ema_fast"] > out["ema_slow"]).astype(int) - (out["ema_fast"] < out["ema_slow"]).astype(int)
    out["ema_slow_slope"]  = ((out["ema_slow"] - out["ema_slow"].shift(1)) / out["ema_slow"].replace(0, pd.NA)).fillna(0.0)

    atr_safe   = out["atr_proxy"].replace(0, pd.NA)
    prev_high  = out["high"].shift(1).rolling(20, min_periods=5).max()
    prev_low   = out["low"].shift(1).rolling(20, min_periods=5).min()
    out["sweep_high_detected"] = ((out["high"] > prev_high) & (out["close"] < prev_high)).astype(int)
    out["sweep_low_detected"]  = ((out["low"] < prev_low)  & (out["close"] > prev_low)).astype(int)
    out["sweep_depth_atr"] = (
        ((out["high"] - prev_high) * out["sweep_high_detected"] + (prev_low - out["low"]) * out["sweep_low_detected"])
        / atr_safe
    ).fillna(0.0)
    out["sweep_rejection_ratio"]  = (out["sweep_high_detected"] * out["upper_wick_ratio"] + out["sweep_low_detected"] * out["lower_wick_ratio"])
    out["sweep_volume_zscore"]     = ((out["sweep_high_detected"] + out["sweep_low_detected"]) * out["volume_zscore"])

    high_2 = out["high"].shift(2)
    low_2  = out["low"].shift(2)
    out["bullish_fvg_present"] = (out["low"]  > high_2).astype(int)
    out["bearish_fvg_present"] = (out["high"] < low_2).astype(int)
    out["fvg_size_atr"] = (
        ((out["low"] - high_2) * out["bullish_fvg_present"] + (low_2 - out["high"]) * out["bearish_fvg_present"])
        / atr_safe
    ).fillna(0.0)
    fvg_event = (out["bullish_fvg_present"] | out["bearish_fvg_present"])
    fvg_group = fvg_event.cumsum()
    out["fvg_age_candles"] = out.groupby(fvg_group).cumcount()
    out.loc[fvg_group == 0, "fvg_age_candles"] = 999.0
    prev_bullish_fvg = out["bullish_fvg_present"].shift(1).fillna(0).astype(bool)
    prev_bearish_fvg = out["bearish_fvg_present"].shift(1).fillna(0).astype(bool)
    out["price_inside_fvg"] = 0
    out.loc[prev_bullish_fvg & (out["close"] < out["low"].shift(1)) & (out["close"] > high_2.shift(1)), "price_inside_fvg"] = 1
    out.loc[prev_bearish_fvg & (out["close"] > out["high"].shift(1)) & (out["close"] < low_2.shift(1)), "price_inside_fvg"] = -1

    out["breakout_volume_confirmation"] = (out["volume_zscore"] * (out["close"] > out["close"].shift(1)).astype(int)).clip(lower=0.0).fillna(0.0)
    out["rejection_volume_confirmation"] = (out["volume_zscore"] * (out["upper_wick_ratio"] + out["lower_wick_ratio"])).clip(lower=0.0).fillna(0.0)

    vol_mean = out["volume"].rolling(20, min_periods=5).mean()
    vol_std  = out["volume"].rolling(20, min_periods=5).std().replace(0, pd.NA)
    vol_z    = ((out["volume"] - vol_mean) / vol_std).fillna(0.0)
    body_quality = (out["body_ratio"].fillna(0.0) * out["range_1"].fillna(0.0)).clip(lower=0.0)
    out["volume_block_strength"] = (vol_z.clip(lower=0.0) * (1.0 + body_quality)).fillna(0.0)

    atr_min = out["atr_proxy"].rolling(100, min_periods=10).min()
    atr_max = out["atr_proxy"].rolling(100, min_periods=10).max()
    out["atr_percentile"] = ((out["atr_proxy"] - atr_min) / (atr_max - atr_min).replace(0, pd.NA)).fillna(0.5)
    rolling_max_20 = out["high"].rolling(20).max()
    rolling_min_20 = out["low"].rolling(20).min()
    out["range_compression"]   = (out["atr_proxy"] / (rolling_max_20 - rolling_min_20).replace(0, pd.NA)).fillna(0.0)
    out["high_volatility_flag"] = (out["atr_proxy"] > out["atr_proxy"].rolling(50).median().fillna(0.0)).astype(int)

    out["market_regime"] = 0
    out.loc[(out["trend_strength"] > 0.002)  & (out["volatility_lookback"] > 0), "market_regime"] = 1
    out.loc[(out["trend_strength"] < -0.002) & (out["volatility_lookback"] > 0), "market_regime"] = -1

    # alias columns that some models may expect
    for exp, calc in [
        ("return_1", "log_return_1"), ("return_3", "log_return_3"), ("return_6", "log_return_6"),
        ("liquidity_sweep_up", "sweep_high_detected"), ("liquidity_sweep_down", "sweep_low_detected"),
        ("fvg_bullish", "bullish_fvg_present"), ("fvg_bearish", "bearish_fvg_present"),
    ]:
        if calc in out.columns and exp not in out.columns:
            out[exp] = out[calc]

    # ── Funding/mark/basis context (broadcast: these are "as of now" live
    # readings, not per-historical-candle series — only the LAST row is
    # ever actually used for a live prediction, so a constant across the
    # buffer is correct here. Absent entirely if funding_ctx is None —
    # that means the columns are simply not added, which the H-13 gate
    # correctly reports as MISSING rather than a fake zero.) ──────────────
    if funding_ctx:
        for col in FUNDING_FEATURE_COLUMNS:
            if col in funding_ctx:
                out[col] = funding_ctx[col]

    # ── Cross-asset peer context (same broadcast rationale as above) ────
    if peer_ctx:
        for prefix, values in peer_ctx.items():
            for suffix in PEER_FEATURE_SUFFIXES:
                if suffix in values:
                    out[f"{prefix}_{suffix}"] = values[suffix]

    # ── Higher-timeframe BTC context (same broadcast rationale as above) ──
    if htf_ctx:
        for col in HTF_FEATURE_COLUMNS:
            if col in htf_ctx:
                out[col] = htf_ctx[col]

    return out


# ── Per-Asset Predictor Engine ────────────────────────────────────────────────
class AssetEngine:
    def __init__(self, symbol: str, cfg: dict):
        self.symbol  = symbol
        self.cfg     = cfg
        self.model_long  = None
        self.model_short = None
        self.feature_names: list[str] = []
        self.missing_features: list[str] = []
        self.degraded = False
        self.feature_gate_result = None          # last ParityResult.to_dict(), for diagnostics
        self.inference_blocked_count = 0          # cumulative count, for observability
        self.last_gate_evaluated_at: float | None = None
        self.candles: list[dict] = []
        self.latest_prediction_long  = 0.0
        self.latest_prediction_short = 0.0
        self.latest_signal = "NEUTRAL"
        self.latest_close: float | None = None   # last traded close, for chat price-level math
        self.latest_atr: float | None = None      # last atr_proxy, for chat price-level math
        self.position = None
        self.trades_history: list[dict] = []
        self.total_pnl   = 0.0
        self.win_trades  = 0
        self.loss_trades = 0
        self.lock = threading.Lock()

        # ── Feature expansion state (funding/mark/basis + cross-asset peers) ──
        # funding_history: rolling window of the last 4 funding snapshots this
        # engine has actually fetched (not last-4-8h-periods — samples are
        # taken on whatever cadence _run_prediction runs at, gated by
        # _FUNDING_SNAPSHOT_TTL_S upstream). Used for funding_rate_mean_4/std_4
        # and mark_premium_mean_4.
        self.funding_history: deque = deque(maxlen=4)
        # latest_basic_ctx: this engine's own last-row return/trend/volume
        # values, published for the OTHER two engines to read as cross-asset
        # peer context — avoids every engine recomputing every other engine's
        # features on every tick. None until this engine has completed at
        # least one prediction cycle.
        self.latest_basic_ctx: Optional[dict] = None
        self.peer_prefixes: list[str] = [
            s.split("/")[0].lower() for s in ASSETS if s != self.symbol
        ]

    def load_models(self):
        long_path  = resolve_model_path(self.cfg["model_long_path"])
        short_path = resolve_model_path(self.cfg["model_short_path"])
        logger.info(f"[{self.symbol}] Loading Long model: {long_path}")
        logger.info(f"[{self.symbol}] Loading Short model: {short_path}")
        self.model_long  = lgb.Booster(model_file=long_path)
        self.model_short = lgb.Booster(model_file=short_path)
        self.feature_names = self.model_long.feature_name()
        logger.success(f"[{self.symbol}] Models loaded — {len(self.feature_names)} features.")

    def load_history(self):
        """Reseed trade stats/history from signal_log's durable SQLite store
        so a restart doesn't visibly wipe a symbol's whole trading record
        back to zero — same numbers as before the restart, just no longer
        memory-only. Best-effort: a fresh install with no prior history is
        expected to come back empty, that's not an error."""
        try:
            stats = signal_log.get_stats(self.symbol)
            self.total_pnl   = stats["total_pnl"]
            self.win_trades  = stats["win_trades"]
            self.loss_trades = stats["loss_trades"]
            rows = signal_log.get_trades(self.symbol, limit=200)
            # DB returns newest-first; trades_history is expected oldest-first
            # (matches append-on-close order), so reverse before loading.
            self.trades_history = [
                {
                    "symbol": r["symbol"], "type": r["direction"],
                    "entry_time": r["entry_time"], "exit_time": r["exit_time"],
                    "entry_price": r["entry_price"], "exit_price": r["exit_price"],
                    "pnl": r["pnl_usdt"], "pnl_pct": r["pnl_pct"], "reason": r["exit_reason"],
                }
                for r in reversed(rows)
            ]
            if self.trades_history:
                logger.info(f"[{self.symbol}] Restored {len(self.trades_history)} trades from durable history "
                            f"({self.win_trades}W/{self.loss_trades}L, {self.total_pnl:+.2f} USDT total).")
        except Exception as ex:
            logger.error(f"[{self.symbol}] Could not restore trade history from signal_log (starting empty): {ex}")

    def fetch_initial_candles(self):
        sym = self.symbol.replace("/", "")
        url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval={TF_MINS}&limit=150"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("retCode") == 0:
                rows = data["result"]["list"]
                rows.reverse()
                with self.lock:
                    self.candles = [
                        {"time": int(row[0]) // 1000, "open": float(row[1]),
                         "high": float(row[2]), "low": float(row[3]),
                         "close": float(row[4]), "volume": float(row[5])}
                        for row in rows
                    ]
                logger.success(f"[{self.symbol}] Loaded {len(self.candles)} historical candles.")
            else:
                logger.error(f"[{self.symbol}] Bybit error: {data}")
        except Exception as e:
            logger.error(f"[{self.symbol}] fetch_initial_candles: {e}")

    def update_candle(self, ts, o, h, l, c, v, confirm, loop):
        with self.lock:
            if not self.candles:
                return
            if self.candles[-1]["time"] == ts:
                self.candles[-1].update({"open": o, "high": h, "low": l, "close": c, "volume": v})
            elif ts > self.candles[-1]["time"]:
                self.candles.append({"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
                if len(self.candles) > 160:
                    self.candles.pop(0)
            self._run_prediction(confirm, loop)

    def _build_funding_ctx(self) -> Optional[dict]:
        """
        Fetch/refresh this engine's funding snapshot (cached upstream), fold
        it into the rolling funding_history, and derive the 8
        FUNDING_FEATURE_COLUMNS values. Returns None if no snapshot is
        available at all yet (e.g. first tick, or Bybit unreachable) — the
        caller must NOT substitute zeros; build_features() simply omits
        these columns in that case, which the H-13 gate reports as missing.
        """
        snap = fetch_funding_snapshot(self.symbol)
        if snap is None and not self.funding_history:
            return None
        if snap is not None:
            mark_basis = (
                (snap["mark_price"] - snap["index_price"]) / snap["index_price"]
                if snap["index_price"] else 0.0
            )
            self.funding_history.append({"funding_rate": snap["funding_rate"], "mark_basis": mark_basis})
        if not self.funding_history:
            return None
        funding_rates = [h["funding_rate"] for h in self.funding_history]
        mark_bases     = [h["mark_basis"]   for h in self.funding_history]
        latest_funding = funding_rates[-1]
        latest_basis   = mark_bases[-1]
        mean4 = sum(funding_rates) / len(funding_rates)
        std4 = (
            (sum((x - mean4) ** 2 for x in funding_rates) / len(funding_rates)) ** 0.5
            if len(funding_rates) > 1 else 0.0
        )
        mark_premium_mean4 = sum(mark_bases) / len(mark_bases)
        return {
            "funding_rate": latest_funding,
            "funding_rate_abs": abs(latest_funding),
            "funding_rate_mean_4": mean4,
            "funding_rate_std_4": std4,
            "mark_basis": latest_basis,
            "mark_premium": latest_basis,
            "mark_premium_mean_4": mark_premium_mean4,
            "futures_pressure": latest_basis - latest_funding,
        }

    def _build_peer_ctx(self) -> Optional[dict]:
        """
        Read the other two engines' last-published basic context. Returns
        None (whole family missing, not partially zero-filled) unless ALL
        configured peers have published at least once — partial cross-asset
        context would be a subtler version of the same fake-data problem
        H-13 was about.
        """
        peer_ctx = {}
        for prefix in self.peer_prefixes:
            peer_symbol = next((s for s in ASSETS if s.split("/")[0].lower() == prefix), None)
            peer_engine = engines.get(peer_symbol) if peer_symbol else None
            if not peer_engine or not peer_engine.latest_basic_ctx:
                return None
            peer_ctx[prefix] = peer_engine.latest_basic_ctx
        return peer_ctx

    def _run_prediction(self, confirm, loop):
        if not self.model_long or not self.model_short or len(self.candles) < 50:
            return
        try:
            funding_ctx = self._build_funding_ctx()
            peer_ctx = self._build_peer_ctx()
            htf_ctx = fetch_btc_htf_context()
            feats = build_features(self.candles, funding_ctx=funding_ctx, peer_ctx=peer_ctx, htf_ctx=htf_ctx)

            # Publish this engine's own basic context for siblings to read as
            # cross-asset peer context on their next tick.
            last_basic = feats.iloc[-1]
            self.latest_basic_ctx = {
                "return_1": float(last_basic["log_return_1"]),
                "return_3": float(last_basic["log_return_3"]),
                "trend": float(last_basic["trend_direction"]),
                "volume_block": float(last_basic["volume_block_strength"]),
            }

            # optional derived features
            if "fvg_bullish_strength" in self.feature_names and "fvg_bullish_strength" not in feats.columns:
                feats["fvg_bullish_strength"] = feats["fvg_size_atr"] * feats["bullish_fvg_present"]
            if "fvg_bearish_strength" in self.feature_names and "fvg_bearish_strength" not in feats.columns:
                feats["fvg_bearish_strength"] = feats["fvg_size_atr"] * feats["bearish_fvg_present"]

            # ── H-13 fail-loud feature parity gate ─────────────────────────
            # Build the row that WOULD be scored (the last candle), then
            # judge it against the model's authoritative feature_names
            # before ever calling predict(). Missing/invalid/stale trained
            # features are never replaced with 0.0 — they block inference.
            last_row = feats.iloc[-1]
            last_time = last_row["time"]
            source_ts = (
                last_time.timestamp() if hasattr(last_time, "timestamp") else float(last_time)
            )
            row_values = {}
            for col in self.feature_names:
                if col in feats.columns:
                    row_values[col] = last_row[col]
                else:
                    row_values[col] = None  # absent -> MISSING, not zero-filled

            gate = evaluate_feature_parity(
                self.feature_names,
                row_values,
                stale_features=None,   # no live source-freshness wiring approved/connected yet (P2-P4 pending)
                source_timestamp=source_ts,
            )
            self.feature_gate_result = gate.to_dict()
            self.last_gate_evaluated_at = time.time()

            gate_changed = (gate.missing != self.missing_features) or (not gate.parity_ok) != self.degraded
            self.missing_features = list(gate.missing)
            self.degraded = not gate.parity_ok

            if gate_changed or not gate.parity_ok:
                log_fn = logger.warning if not gate.parity_ok else logger.success
                log_fn(f"[{self.symbol}] {format_gate_log_summary(gate)}")

            if not gate.parity_ok:
                # Fail loud: no model call, no BUY/SELL/NEUTRAL/EXIT signal.
                self.inference_blocked_count += 1
                old_sig = self.latest_signal
                self.latest_signal = "UNAVAILABLE"
                if confirm and old_sig != self.latest_signal:
                    logger.warning(
                        f"[{self.symbol}] Signal UNAVAILABLE — inference blocked: {gate.blocked_reason}"
                    )
                asyncio.run_coroutine_threadsafe(
                    manager.broadcast({
                        "type": "tick",
                        "symbol": self.symbol,
                        "candle": self.candles[-1],
                        "prediction_long":  self.latest_prediction_long,
                        "prediction_short": self.latest_prediction_short,
                        "signal": self.latest_signal,
                        "position": self.position,
                        "degraded": self.degraded,
                        "missing_features": self.missing_features,
                        "feature_gate": self.feature_gate_result,
                        "stats": self._stats(),
                        "latest_close": self.latest_close,
                        "latest_atr": self.latest_atr,
                    }),
                    loop,
                )
                return

            X = feats[self.feature_names].astype(float)
            self.latest_prediction_long  = float(self.model_long.predict(X)[-1])
            self.latest_prediction_short = float(self.model_short.predict(X)[-1])

            # Signal logic
            old_sig = self.latest_signal
            if not self.position:
                if self.latest_prediction_long  >= self.cfg["buy_threshold"]:
                    self.latest_signal = "BUY"
                elif self.latest_prediction_short >= self.cfg["sell_threshold"]:
                    self.latest_signal = "SELL"
                else:
                    self.latest_signal = "NEUTRAL"
            else:
                if self.position["type"] == "LONG" and self.latest_prediction_long < self.cfg["exit_threshold"]:
                    self.latest_signal = "EXIT"
                elif self.position["type"] == "SHORT" and self.latest_prediction_short < self.cfg["exit_short_threshold"]:
                    self.latest_signal = "EXIT"
                else:
                    self.latest_signal = "NEUTRAL"

            last = feats.iloc[-1]
            self.latest_close = float(last["close"])
            self.latest_atr   = float(last["atr_proxy"])
            self._update_sim(self.latest_close, last["time"], self.latest_atr, confirm)

            asyncio.run_coroutine_threadsafe(
                manager.broadcast({
                    "type": "tick",
                    "symbol": self.symbol,
                    "candle": self.candles[-1],
                    "prediction_long":  self.latest_prediction_long,
                    "prediction_short": self.latest_prediction_short,
                    "signal": self.latest_signal,
                    "position": self.position,
                    "degraded": self.degraded,
                    "missing_features": self.missing_features,
                    "feature_gate": self.feature_gate_result,
                    "stats": self._stats(),
                    "latest_close": self.latest_close,
                    "latest_atr": self.latest_atr,
                }),
                loop,
            )
            if confirm and old_sig != self.latest_signal:
                logger.info(f"[{self.symbol}] Signal: {self.latest_signal} | L={self.latest_prediction_long:.4f} S={self.latest_prediction_short:.4f}")

            if confirm:
                # Full activity log, every confirmed tick — not just
                # transitions. A transition-only log would miss most of the
                # actual signal (e.g. hours of NEUTRAL between two BUYs),
                # and this data's whole purpose is to eventually support
                # retraining/threshold-calibration work, which needs the
                # continuous prediction trajectory, not just the moments it
                # changed.
                try:
                    ts_int = int(last["time"].timestamp()) if hasattr(last["time"], "timestamp") else int(last["time"])
                    signal_log.record_signal_event(
                        ts=ts_int, symbol=self.symbol, signal=self.latest_signal,
                        long_prob=self.latest_prediction_long, short_prob=self.latest_prediction_short,
                        price=self.latest_close, atr=self.latest_atr, degraded=self.degraded,
                    )
                except Exception as log_ex:
                    logger.error(f"[{self.symbol}] signal_log.record_signal_event failed (tick NOT durably saved): {log_ex}")
        except Exception as ex:
            logger.error(f"[{self.symbol}] prediction error: {ex}")

    def _update_sim(self, price, ts, atr, confirm):
        spread = price * self.cfg["spread_offset_pct"]
        bid    = price - spread / 2
        ask    = price + spread / 2
        if self.position:
            pos = self.position
            if pos["type"] == "LONG":
                exit_price = bid
                pnl = (bid - pos["entry_price"]) / pos["entry_price"]
                hit_tp = bid >= pos["tp"]
                hit_sl = bid <= pos["sl"]
            else:
                exit_price = ask
                pnl = (pos["entry_price"] - ask) / pos["entry_price"]
                hit_tp = ask <= pos["tp"]
                hit_sl = ask >= pos["sl"]

            reason = None
            if hit_tp:
                reason = "Take Profit"
            elif hit_sl:
                reason = "Stop Loss"
            elif self.latest_signal == "EXIT":
                reason = "Exit Signal"

            if confirm:
                pos["candles_held"] += 1
                if pos["candles_held"] >= self.cfg.get("max_candles_held", 4):
                    reason = "Time Decay"

            if reason:
                pnl_usdt = 100.0 * pnl
                self.total_pnl += pnl_usdt
                if pnl >= 0: self.win_trades += 1
                else: self.loss_trades += 1
                ts_int = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
                trade_row = {
                    "symbol": self.symbol,
                    "type": pos["type"],
                    "entry_time": pos["entry_time"],
                    "exit_time": ts_int,
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_price,
                    "pnl": pnl_usdt,
                    "pnl_pct": pnl,
                    "reason": reason,
                }
                self.trades_history.append(trade_row)
                try:
                    signal_log.record_trade(trade_row)
                except Exception as log_ex:
                    # Never let a logging failure break live trading logic —
                    # but do surface it loudly, since this is the exact kind
                    # of silent-data-loss this module exists to prevent.
                    logger.error(f"[{self.symbol}] signal_log.record_trade failed (trade NOT durably saved): {log_ex}")
                logger.success(f"[{self.symbol}] {pos['type']} EXIT | {reason} | PnL {pnl_usdt:+.2f} USDT")
                self.position = None
        elif confirm:
            ts_int = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
            if self.latest_signal == "BUY":
                self.position = {
                    "type": "LONG", "entry_time": ts_int, "entry_price": ask,
                    "tp": ask + self.cfg["tp_atr_mult"] * atr,
                    "sl": ask - self.cfg["sl_atr_mult"] * atr, "candles_held": 0,
                }
            elif self.latest_signal == "SELL":
                self.position = {
                    "type": "SHORT", "entry_time": ts_int, "entry_price": bid,
                    "tp": bid - self.cfg["tp_atr_mult"] * atr,
                    "sl": bid + self.cfg["sl_atr_mult"] * atr, "candles_held": 0,
                }

    def _stats(self):
        return {
            "total_pnl": self.total_pnl,
            "win_trades": self.win_trades,
            "loss_trades": self.loss_trades,
            "total_trades": len(self.trades_history),
        }

    def snapshot(self):
        """Thread-safe snapshot for REST endpoints."""
        with self.lock:
            return {
                "symbol": self.symbol,
                "status": "online",
                "latest_prediction_long":  self.latest_prediction_long,
                "latest_prediction_short": self.latest_prediction_short,
                "latest_signal": self.latest_signal,
                "position": self.position,
                "degraded": self.degraded,
                "missing_features": list(self.missing_features),
                "feature_gate": self.feature_gate_result,
                "inference_blocked_count": self.inference_blocked_count,
                "last_gate_evaluated_at": self.last_gate_evaluated_at,
                "candles": list(self.candles),
                "trades": list(self.trades_history),
                "stats": self._stats(),
                "latest_close": self.latest_close,
                "latest_atr": self.latest_atr,
            }


# ── Create engines ────────────────────────────────────────────────────────────
engines: dict[str, AssetEngine] = {
    sym: AssetEngine(sym, cfg) for sym, cfg in config["assets"].items()
}

# ── Enriched signal store (written by the Hermes signal agent) ────────────────
# Keyed by normalised symbol, e.g. "BTC/USDT"
_enriched_signals: dict[str, dict] = {}

# ── FastAPI App ───────────────────────────────────────────────────────────────
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
DASHBOARD_ORIGINS = [
    o.strip() for o in os.getenv("DASHBOARD_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
    if o.strip()
]

app = FastAPI(title="Antigravity Predictor v2")
app.add_middleware(CORSMiddleware, allow_origins=DASHBOARD_ORIGINS,
                   allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

def require_internal_token(x_internal_token: str = Header(default="")) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="INTERNAL_API_TOKEN is not configured")
    if x_internal_token != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid internal token")

loop: asyncio.AbstractEventLoop | None = None

# ── Bybit WebSocket listener (one connection, all symbols) ────────────────────
async def poll_bybit_websocket():
    ws_url = "wss://stream.bybit.com/v5/public/linear"
    topics = [f"kline.{TF_MINS}.{sym.replace('/', '')}" for sym in ASSETS]
    # build reverse map: "kline.15.BTCUSDT" -> "BTC/USDT"
    topic_map = {f"kline.{TF_MINS}.{sym.replace('/', '')}": sym for sym in ASSETS}

    logger.info(f"Connecting to Bybit WebSocket — topics: {topics}")
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                logger.success("Subscribed to all asset streams.")
                async for message in ws:
                    data = json.loads(message)
                    topic = data.get("topic", "")
                    if topic in topic_map and "data" in data:
                        k = data["data"][0]
                        sym = topic_map[topic]
                        engines[sym].update_candle(
                            int(k["start"]) // 1000,
                            float(k["open"]), float(k["high"]),
                            float(k["low"]),  float(k["close"]),
                            float(k["volume"]), k["confirm"], loop,
                        )
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in 5s…")
            await asyncio.sleep(5)

def run_ws_loop():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(poll_bybit_websocket())

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup_event():
    signal_log.init_db()
    for eng in engines.values():
        eng.load_models()
        eng.load_history()
        eng.fetch_initial_candles()
    threading.Thread(target=run_ws_loop, daemon=True).start()
    logger.success("All engines started — Predictor v2 online.")


def fetch_display_candles(symbol: str, timeframe: str, limit: int = 300) -> list[dict]:
    """Fetch display candles for the dashboard timeframe selector.

    This does not change the model's configured prediction timeframe; it only
    serves chart display data for the client-facing dashboard.
    """
    tf = normalise_timeframe(timeframe)
    if is_macro_display_asset(symbol):
        if tf != "1d":
            raise HTTPException(status_code=400, detail="Gold/XAU is available as a real daily macro feed only")
        return fetch_gold_daily_candles(limit=limit)
    sym = symbol.replace("/", "")
    interval = bybit_interval(tf)
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval={interval}&limit={max(1, min(limit, 1000))}"
    r = requests.get(url, timeout=10)
    data = r.json()
    if data.get("retCode") != 0:
        raise HTTPException(status_code=502, detail=f"Bybit candle fetch failed: {data}")
    rows = data["result"]["list"]
    rows.reverse()
    return [
        {"time": int(row[0]) // 1000, "open": float(row[1]),
         "high": float(row[2]), "low": float(row[3]),
         "close": float(row[4]), "volume": float(row[5])}
        for row in rows
    ]


def bybit_symbol(symbol: str) -> str:
    if is_macro_display_asset(symbol):
        raise HTTPException(status_code=400, detail="Gold/XAU is not a Bybit linear market in this dashboard")
    sym = symbol if symbol in engines else "BTC/USDT"
    return sym.replace("/", "")

@app.get("/api/orderbook")
def get_orderbook(symbol: Optional[str] = Query(default="BTC/USDT"), limit: int = Query(default=10)):
    sym = symbol if symbol in engines else "BTC/USDT"
    lim = max(1, min(limit, 50))
    url = "https://api.bybit.com/v5/market/orderbook"
    r = requests.get(url, params={"category": "linear", "symbol": bybit_symbol(sym), "limit": lim}, timeout=10)
    data = r.json()
    if data.get("retCode") != 0:
        raise HTTPException(status_code=502, detail=f"Bybit orderbook fetch failed: {data}")
    result = data.get("result", {})
    return {
        "symbol": sym,
        "source": "bybit",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bids": [{"price": float(p), "size": float(q)} for p, q in result.get("b", [])[:lim]],
        "asks": [{"price": float(p), "size": float(q)} for p, q in result.get("a", [])[:lim]],
    }

@app.get("/api/market-tickers")
def get_market_tickers():
    url = "https://api.bybit.com/v5/market/tickers"
    r = requests.get(url, params={"category": "linear"}, timeout=10)
    data = r.json()
    if data.get("retCode") != 0:
        raise HTTPException(status_code=502, detail=f"Bybit tickers fetch failed: {data}")
    wanted = {bybit_symbol(sym): sym for sym in engines}
    out = []
    for row in data.get("result", {}).get("list", []):
        raw = row.get("symbol")
        if raw not in wanted:
            continue
        out.append({
            "symbol": wanted[raw],
            "last_price": float(row.get("lastPrice") or 0),
            "change_24h": float(row.get("price24hPcnt") or 0) * 100,
            "turnover_24h": float(row.get("turnover24h") or 0),
            "volume_24h": float(row.get("volume24h") or 0),
            "source": "bybit",
        })
    try:
        gold = fetch_gold_daily_candles(limit=2)
        if gold:
            last = gold[-1]
            prev = gold[-2] if len(gold) > 1 else last
            pct = ((last["close"] - prev["close"]) / prev["close"] * 100) if prev["close"] else 0.0
            out.append({
                "symbol": "XAU/USD",
                "last_price": last["close"],
                "change_24h": pct,
                "turnover_24h": 0,
                "volume_24h": last.get("volume", 0),
                "source": "macro_gold_parquet",
            })
    except Exception as exc:
        now = time.time()
        if (now - _GOLD_WARN_STATE["last_warned_at"]) >= _GOLD_WARN_INTERVAL_S:
            suppressed = _GOLD_WARN_STATE["suppressed_count"]
            suffix = f" ({suppressed} further occurrences suppressed in the last {int(_GOLD_WARN_INTERVAL_S)}s)" if suppressed else ""
            logger.warning(f"Gold macro ticker unavailable: {exc}{suffix}")
            _GOLD_WARN_STATE["last_warned_at"] = now
            _GOLD_WARN_STATE["suppressed_count"] = 0
        else:
            _GOLD_WARN_STATE["suppressed_count"] += 1
    return {"source": "bybit+macro", "assets": out}

@app.get("/api/news")
def get_news(limit: int = Query(default=8)):
    feeds = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
    ]
    items = []
    for feed_url in feeds:
        try:
            r = requests.get(feed_url, timeout=8, headers={"User-Agent": "AntigravityPredictor/1.0"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            channel_items = root.findall(".//item")
            for item in channel_items:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                source = "CoinDesk" if "coindesk" in feed_url else "Cointelegraph"
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    items.append({"title": title, "source": source, "published": pub, "url": link})
                if len(items) >= limit:
                    break
        except Exception as e:
            logger.warning(f"news feed failed {feed_url}: {e}")
        if len(items) >= limit:
            break
    return {"source": "rss", "items": items[:max(1, min(limit, 20))]}

@app.get("/api/calendar")
def get_calendar(limit: int = Query(default=8)):
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
    items = []
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "AntigravityPredictor/1.0"})
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            for event in root.findall(".//event"):
                title = (event.findtext("title") or "").strip()
                country = (event.findtext("country") or "").strip()
                date = (event.findtext("date") or "").strip()
                time_txt = (event.findtext("time") or "").strip()
                impact = (event.findtext("impact") or "").strip()
                if title:
                    items.append({"title": title, "country": country, "date": date, "time": time_txt, "impact": impact, "source": "forexfactory"})
                if len(items) >= limit:
                    break
    except Exception as e:
        logger.warning(f"calendar feed failed: {e}")
    return {"source": "forexfactory", "items": items[:max(1, min(limit, 20))]}

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/api/status")
def get_status(symbol: Optional[str] = Query(default=None)):
    if symbol and symbol in engines:
        snap = engines[symbol].snapshot()
        return {
            "status": "online", "exchange": config["exchange"],
            "symbol": symbol, "timeframe": TIMEFRAME,
            "latest_prediction":       snap["latest_prediction_long"],
            "latest_prediction_long":  snap["latest_prediction_long"],
            "latest_prediction_short": snap["latest_prediction_short"],
            "latest_signal": snap["latest_signal"],
            "position": snap["position"],
            "degraded": snap["degraded"],
            "missing_features": snap["missing_features"],
            "feature_gate": snap["feature_gate"],
            "inference_blocked_count": snap["inference_blocked_count"],
            "stats": snap["stats"],
            "latest_close": snap["latest_close"],
            "latest_atr": snap["latest_atr"],
        }
    # Return summary for all assets
    return {
        "status": "online",
        "exchange": config["exchange"],
        "timeframe": TIMEFRAME,
        "assets": {
            sym: {
                "latest_prediction_long":  eng.latest_prediction_long,
                "latest_prediction_short": eng.latest_prediction_short,
                "latest_signal": eng.latest_signal,
                "position": eng.position,
                "degraded": eng.degraded,
                "missing_features": eng.missing_features,
                "inference_blocked_count": eng.inference_blocked_count,
                "stats": eng._stats(),
            } for sym, eng in engines.items()
        }
    }


@app.get("/api/signal-history")
def get_signal_history(symbol: Optional[str] = Query(default=None),
                        limit: int = Query(default=500, le=5000),
                        format: str = Query(default="json")):
    """Full durable activity log — every confirmed prediction tick (not
    just BUY/SELL transitions), independent of process restarts. This is
    the raw material for retraining/threshold-calibration work, not just a
    live dashboard number. Backed by signal_log.py / logs/signal_history.db.
    """
    rows = signal_log.get_signal_events(symbol=symbol, limit=limit)
    if format == "csv":
        import csv, io
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                  headers={"Content-Disposition": "attachment; filename=signal_history.csv"})
    return {"symbol": symbol, "count": len(rows), "events": rows}


@app.get("/api/trade-history")
def get_trade_history(symbol: Optional[str] = Query(default=None),
                       limit: int = Query(default=500, le=5000),
                       format: str = Query(default="json")):
    """Full durable record of every completed simulated trade, independent
    of process restarts. See /api/signal-history for the underlying every-
    tick log this is derived from."""
    rows = signal_log.get_trades(symbol=symbol, limit=limit)
    if format == "csv":
        import csv, io
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                  headers={"Content-Disposition": "attachment; filename=trade_history.csv"})
    return {"symbol": symbol, "count": len(rows), "trades": rows}


@app.get("/api/feature-parity/{symbol}")
def get_feature_parity(symbol: str):
    """H-13 diagnostic endpoint (P1 observability requirement).

    Exposes expected/populated feature counts, missing/invalid/stale
    feature names, per-family status, whether inference is currently
    blocked, and the timestamp of the source candle used for the last
    gate evaluation. No secrets or credentials are exposed here.
    """
    sym = symbol.replace("_", "/")
    sym = sym if sym in engines else symbol
    if sym not in engines:
        raise HTTPException(status_code=404, detail=f"Unknown asset: {symbol}")
    eng = engines[sym]
    with eng.lock:
        gate = eng.feature_gate_result
        return {
            "symbol": sym,
            "expected_features": len(eng.feature_names),
            "gate": gate,
            "degraded": eng.degraded,
            "current_signal": eng.latest_signal,
            "inference_blocked": eng.degraded,
            "inference_blocked_count": eng.inference_blocked_count,
            "last_gate_evaluated_at": eng.last_gate_evaluated_at,
        }

@app.get("/api/candles")
def get_candles(symbol: Optional[str] = Query(default="BTC/USDT"), timeframe: Optional[str] = Query(default=None), limit: int = Query(default=300)):
    if is_macro_display_asset(symbol):
        tf = normalise_timeframe(timeframe or "1d")
        if tf != "1d":
            raise HTTPException(status_code=400, detail="Gold/XAU is available as a real daily macro feed only")
        return fetch_gold_daily_candles(limit=limit)
    sym = symbol if symbol in engines else "BTC/USDT"
    tf = normalise_timeframe(timeframe)
    if tf == TIMEFRAME:
        return engines[sym].snapshot()["candles"][-max(1, min(limit, 1000)):]
    return fetch_display_candles(sym, tf, limit=limit)

@app.get("/api/trades")
def get_trades(symbol: Optional[str] = Query(default=None)):
    if symbol and symbol in engines:
        return engines[symbol].snapshot()["trades"]
    # All trades combined, sorted by exit_time descending
    all_trades = []
    for eng in engines.values():
        all_trades.extend(eng.trades_history)
    return sorted(all_trades, key=lambda t: t.get("exit_time", 0), reverse=True)

@app.get("/api/assets")
def get_assets():
    return {"assets": DISPLAY_ASSETS}

# ── Enriched signal endpoints (Hermes signal agent ↔ dashboard) ───────────────

@app.post("/api/enriched-signal/{asset:path}")
async def post_enriched_signal(asset: str, payload: dict, _: None = Depends(require_internal_token)):
    """
    Written by the Hermes signal agent when a high-confidence event fires.
    `asset` accepts 'BTC_USDT', 'BTC%2FUSDT' (percent-encoded slash), or a
    literal 'BTC/USDT' — the ':path' converter is required for the last
    form to route at all; FastAPI's default single-segment {asset} string
    param silently fails to match anything containing a real '/' and falls
    through to the static-file mount instead (405, not this handler).
    """
    # Accept both BTC_USDT and BTC/USDT spellings from callers
    sym = asset.replace("_", "/")
    if sym not in engines and asset not in engines:
        raise HTTPException(status_code=404, detail=f"Unknown asset: {asset}")
    sym = sym if sym in engines else asset

    payload["received_at"] = datetime.now(timezone.utc).isoformat()
    _enriched_signals[sym] = payload
    logger.info(f"[signal-agent] Enriched signal received for {sym}: {payload.get('signal')} | {payload.get('confidence')}")

    # Broadcast to all dashboard WS clients
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type": "enriched_signal", "asset": sym, "signal": payload}),
            loop,
        )

    return {"status": "ok", "asset": sym}


@app.get("/api/enriched-signal/{asset:path}")
def get_enriched_signal(asset: str):
    """
    Read by the dashboard to display the latest Hermes-enriched signal.
    Returns 204 No Content if no signal has been posted yet.
    """
    sym = asset.replace("_", "/")
    sym = sym if sym in _enriched_signals else asset
    sig = _enriched_signals.get(sym)
    if sig is None:
        return JSONResponse(status_code=204, content=None)
    return sig


@app.get("/api/enriched-signals")
def get_all_enriched_signals():
    """All enriched signals keyed by asset."""
    return _enriched_signals


# ── Hermes Chat endpoint ─────────────────────────────────────────────────────
#
# One chat surface, one system prompt (grounded in live engine data — signal,
# probabilities, position, stats, model metrics), swappable backend. No
# execution tools are ever exposed; pure text in/out, for any backend.
#
#   /api/chat — "Hermes", the Crypto Operator Agent. Terse, advisory-only
#               commentary on the current signal AND — when asked — explains
#               general concepts and advises on performance/threshold
#               improvements grounded in the actual model metrics on disk.
#               Zero execution access: no tool-calling is wired up at all, so
#               this is a structural guarantee, not a prompt-only promise.
#
# Ships with Hermes (an OpenAI-compatible proxy, see .env.example) as the
# default backend, but a client can point it at a different agent without
# touching code: set CHAT_BACKEND to "hermes_proxy" | "anthropic" | "ollama"
# to force one explicitly, or leave it unset to auto-detect in that order
# (see _backend_config()/_call_llm_backend()). This is deliberately narrower
# than "accept any framework's request format" — the system prompt/context
# assembly (what makes this chat useful) stays fixed; only the outbound
# wire protocol to the backend is swappable. An earlier draft of this
# also proposed a generic messages[]-in/choices[]-out contract so arbitrary
# external frameworks (LangChain, CrewAI, etc.) could call *into* /api/chat
# directly — rejected as scope creep with no concrete driver; nothing in
# this system currently needs to be called by a third-party framework, only
# to itself call a client's choice of backend.
#
# Previously split into two personas/endpoints (this one plus a separate
# /api/tutor-chat "Hermes Tutor"). Merged back into one 2026-07-23: the
# operator is the client-facing persona actually in use, the split added a
# second system prompt/memory file/set of env overrides to keep in sync for
# a distinction real users couldn't reliably tell apart from two chat boxes
# on the same dashboard — and the dashboard's own JS had silently let
# initAdvisoryChat()'s node-cloning clobber initTutorChat()'s button
# listener on load anyway, so /api/tutor-chat was already effectively dead
# from the UI's perspective before this merge. GET /api/chat/status reports
# what's actually wired so this doesn't have to be reverse-engineered from
# env vars.

class _ChatMsg(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class _ChatRequest(BaseModel):
    message: str
    symbol: str = "BTC/USDT"
    language: str = "en"
    history: List[_ChatMsg] = []


def _compute_price_levels(signal: str, close: float, atr: float) -> Optional[dict]:
    """
    Server-side mirror of dashboard/app.js's updatePriceLevels() — same
    multipliers as the training labels (TP1=1.5xATR, TP2=2.5xATR, SL=1.0xATR)
    and the same ~0.15% round-trip fee drag, so a number the chat states
    matches the number the dashboard's Agent Report panel shows for the same
    tick. Returns None for NEUTRAL/UNAVAILABLE/EXIT or missing close/atr —
    there's no valid entry to size in those states, and the chat should say
    so rather than inventing numbers.
    """
    if signal not in ("BUY", "SELL") or not close or not atr or atr <= 0:
        return None
    is_long = signal == "BUY"
    tp1_mult, tp2_mult, sl_mult = 1.5, 2.5, 1.0
    fee_drag = close * 0.0015
    if is_long:
        entry = close
        sl  = close - sl_mult  * atr - fee_drag
        tp1 = close + tp1_mult * atr - fee_drag
        tp2 = close + tp2_mult * atr - fee_drag
    else:
        entry = close
        sl  = close + sl_mult  * atr + fee_drag
        tp1 = close - tp1_mult * atr + fee_drag
        tp2 = close - tp2_mult * atr + fee_drag
    risk   = abs(entry - sl)
    reward = abs(tp1 - entry)
    rr     = round(reward / risk, 2) if risk > 0 else None
    return {
        "entry": round(entry, 2), "stop_loss": round(sl, 2),
        "tp1": round(tp1, 2), "tp2": round(tp2, 2),
        "atr": round(atr, 2), "risk_reward_tp1": rr,
    }


def _format_price_levels_for_prompt(levels: Optional[dict]) -> str:
    if not levels:
        return "No active BUY/SELL signal right now, so there is no entry/stop-loss/take-profit to state — say so plainly rather than inventing numbers."
    rr_str = f"{levels['risk_reward_tp1']}:1" if levels['risk_reward_tp1'] else "n/a"
    return (
        f"Entry ${levels['entry']:,.2f} | Stop Loss ${levels['stop_loss']:,.2f} | "
        f"TP1 (1.5x ATR) ${levels['tp1']:,.2f} | TP2 (2.5x ATR) ${levels['tp2']:,.2f} | "
        f"ATR ${levels['atr']:,.2f} | R:R (to TP1) {rr_str}"
    )


def _backend_config() -> dict:
    """Thin alias — the actual backend resolution lives in llm_backend.py
    now, shared with signal_agent/enricher.py's automated enrichment (same
    brain, one place that decides which backend answers, for both)."""
    return llm_backend.backend_config()


def _call_llm_backend(system_ctx: str, message: str, history: List[_ChatMsg], cfg: dict) -> Optional[dict]:
    """Thin alias over llm_backend.call_llm() — kept so call sites below
    don't need touching. See llm_backend.py for the actual implementation
    shared with signal_agent/enricher.py."""
    messages_tail = [{"role": h.role, "content": h.content} for h in history[-8:]]
    return llm_backend.call_llm(system_ctx, message, messages_tail, cfg)


@app.post("/api/chat")
async def hermes_chat(req: _ChatRequest):
    """
    Hermes signal-agent interactive chat. Thin async wrapper — the actual
    work happens in _hermes_chat_sync(), run off the event loop via
    asyncio.to_thread().

    Why this indirection: _hermes_chat_sync() calls out to the configured
    backend via llm_backend.py, which uses the synchronous `requests`
    library (requests.post(..., timeout=60)) — a real, blocking network
    call. Every backend tested during initial development (an echo stub,
    a direct proxy call) replied in milliseconds, so calling that
    synchronous code directly from this async def never visibly blocked
    anything. Once a real agent was wired in — one that takes actual
    seconds to think, not milliseconds — that same blocking call started
    stalling uvicorn's entire event loop for the duration of every /api/chat
    request. Found live: with the event loop stalled, this predictor
    process's other concurrent async work (the dashboard's WebSocket
    connection in particular) got starved, and the interleaving corruption
    that produced surfaced as `h11._util.LocalProtocolError: Too much data
    for declared Content-Length` — a transport-level symptom that looked
    unrelated to this endpoint at first, reproduced only under real
    concurrent load with a real (non-instant) backend, not under an
    isolated single-request test with a fast stub. Confirmed by reproducing
    predictor's exact /api/chat response shape (same Spanish-accented reply
    text, same price_levels dict) under a real uvicorn server and finding
    it could NOT be triggered by content/encoding alone -- ruling that out
    left "the handler blocks the event loop" as the remaining explanation,
    which matches every observed symptom (fine under fast stubs, breaks
    under load with the first genuinely slow real backend).

    asyncio.to_thread() runs the whole synchronous body (engine lock,
    _live_system_context()'s own requests.get() calls, memory recall/
    append, and the backend call) in a worker thread, freeing the event
    loop to keep servicing other connections (WebSocket included) while
    this request waits on a real agent to answer.
    """
    return await asyncio.to_thread(_hermes_chat_sync, req)


def _hermes_chat_sync(req: _ChatRequest):
    """
    Synchronous body of /api/chat -- see hermes_chat()'s docstring for why
    this is invoked via asyncio.to_thread() rather than being the route
    handler directly. Returns a context-aware reply from a configured real
    agent/LLM backend. If no backend is reachable, returns 503
    agent_unavailable; there is no scripted success fallback.
    """
    sym = req.symbol if req.symbol in engines else "BTC/USDT"
    eng = engines[sym]

    with eng.lock:
        signal     = eng.latest_signal
        long_prob  = eng.latest_prediction_long
        short_prob = eng.latest_prediction_short
        pos        = eng.position
        stats      = eng._stats()
        close      = eng.latest_close
        atr        = eng.latest_atr

    price_levels = _compute_price_levels(signal, close, atr)
    enriched   = _enriched_signals.get(sym, {})
    pos_str    = (
        f"{pos['type']} @ {pos['entry_price']:.2f} | TP {pos['tp']:.2f} | SL {pos['sl']:.2f}"
        if pos else "flat"
    )

    # Only override the persona's own Spanish-by-default behavior if the
    # dashboard explicitly told us the client's language is English.
    lang = "en" if str(req.language).lower().startswith("en") and req.language else "es"
    language_rule = (
        "The dashboard reports this client's UI language as English — respond in English unless "
        "their message is clearly in Spanish, in which case follow them into Spanish."
        if lang == "en"
        else "Respond in Spanish by default, per your persona above."
    )

    memory_recall = _operator_memory_recall()
    system_ctx = (
        _CRYPTO_OPERATOR_SYSTEM_PROMPT
        + f"\n\n[Live engine data — {sym}]\nsignal={signal}, long_prob={long_prob:.4f}, "
        + f"short_prob={short_prob:.4f}, position={pos_str}, "
        + f"total_trades={stats['total_trades']}, win_trades={stats['win_trades']}, "
        + f"net_pnl={stats['total_pnl']:.2f} USDT."
        + (
            f"\n\n[Your own earlier automated signal note for {sym} — you generated this, it's not "
            f"from anyone else. Speak to it naturally if asked, in plain sentences, never as a data "
            f"dump.]\nWhat you noted: {enriched.get('analyst_note', 'nothing noted yet')}\n"
            f"News context you had: {enriched.get('news_summary', 'none')}\n"
            f"Risks you flagged: {enriched.get('key_risks', 'none')}"
            if enriched else ""
        )
        + f"\n\n[Trade levels for the current signal — these are the ACTUAL computed numbers, "
        + "the same ones shown on the dashboard's Agent Report panel. When you discuss this "
        + "signal or suggest a trade, state these exact dollar amounts — never speak only in "
        + f"qualitative terms when real numbers are available.]\n{_format_price_levels_for_prompt(price_levels)}"
        + f"\n\n[Full system context — read-only]\n{_live_system_context()}"
        + f"\n\n[Model performance context — cite these real numbers, don't invent them, when asked "
        + f"about thresholds/retraining/performance]\n{_model_performance_context()}"
        + (f"\n\n[Recalled memory from earlier conversations with this client]\n{memory_recall}" if memory_recall else "")
        + f"\n\n[Language]\n{language_rule}"
    )

    result = _call_llm_backend(system_ctx, req.message, req.history, _backend_config())
    if result:
        _operator_memory_append(req.message, result["reply"])
        return {
            "reply": result["reply"],
            "source": result["source"],
            "signal": signal,
            "price_levels": price_levels,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    return JSONResponse(
        status_code=503,
        content={
            "error": "agent_unavailable",
            "message": "Agent backend unavailable or disabled",
            "source": "unavailable",
        },
    )


def _model_performance_context() -> str:
    """
    Reads whatever model report/config files exist on disk and summarizes
    them for the chat prompt. Missing files are skipped silently (not every
    deployment will have run every tool) rather than raising — this is
    advisory context, not a hard dependency.
    """
    lines = []
    models_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models")
    for fname, label in [
        ("recalibrate_thresholds_report.json", "Current threshold calibration"),
        ("retrain_live_features_report.json", "Last retrain test metrics"),
    ]:
        path = os.path.join(models_dir, fname)
        try:
            with open(path) as f:
                data = json.load(f)
            for model_name, m in data.items():
                if "actual_fire_rate" in m:
                    lines.append(
                        f"{model_name}: entry={m['entry_threshold']} exit={m['exit_threshold']} "
                        f"fire_rate={m['actual_fire_rate']:.0%}"
                    )
                elif "test_auc" in m:
                    lines.append(
                        f"{model_name}: test_auc={m['test_auc']:.3f} "
                        f"test_precision={m['test_precision']:.3f} "
                        f"fire_rate={m.get('test_fire_rate', 0):.0%}"
                    )
        except Exception:
            continue
    if not lines:
        return "No model metrics files found on disk."
    return "\n".join(lines)


def _live_system_context() -> str:
    """
    Broader read-only situational awareness for the chat: per-asset feature
    parity, executor dry-run status, and Forge's current leaderboard leader
    (if reachable). This is still purely informational — the chat has no
    tool wired to act on any of it. Every lookup is best-effort: a service
    being unreachable just means that section is omitted, never an error
    surfaced to the user.
    """
    lines = []
    for sym, eng in engines.items():
        with eng.lock:
            lines.append(
                f"{sym}: signal={eng.latest_signal}, degraded={eng.degraded}, "
                f"missing_features={len(eng.missing_features)}, "
                f"inference_blocked_count={eng.inference_blocked_count}"
            )
    try:
        r = requests.get("http://127.0.0.1:18911/health", timeout=1.5)
        if r.status_code == 200:
            h = r.json()
            lines.append(f"Executor: dry_run={h.get('dry_run')}, open_positions={h.get('open_positions')}")
    except Exception:
        pass
    try:
        r = requests.get("http://127.0.0.1:18912/leaderboard", timeout=1.5)
        if r.status_code == 200:
            board = r.json()
            if board:
                top = board[0]
                lines.append(
                    f"Forge leaderboard leader: {top.get('strategy_name')} "
                    f"win_rate={top.get('win_rate')} net_pnl={top.get('net_pnl')}"
                )
    except Exception:
        pass
    return "\n".join(lines) if lines else "No live system data available."


# Memory + core identity now live in hermes_persona.py, shared with
# signal_agent/enricher.py's automated enrichment — same brain, same
# memory, whether it's chatting with the client or generating an automated
# signal note (see hermes_persona.py's module docstring for why).
def _operator_memory_recall() -> str:
    return hermes_persona.memory_recall()


def _operator_memory_append(user_msg: str, reply: str) -> None:
    hermes_persona.memory_append(user_msg, reply)


_CRYPTO_OPERATOR_SYSTEM_PROMPT = hermes_persona.HERMES_CORE_IDENTITY + """

On the dashboard, you're wired directly to the system's live back engine,
talking with a dedicated client who communicates primarily in Spanish,
using crypto slang and informal market jargon. Respond in Spanish by
default, matching a natural, competent-operator register; follow the
client if they switch language.

You ARE given real, current engine data every turn (signal, probabilities,
position, stats, feature-gate health) below, and you should use it
directly rather than hedge about signals you've actually been given. If
something isn't in the context you were given this turn, say so rather
than inferring or fabricating it.

Core traits, non-negotiable:
- Patient: never rush the client or make them feel slow for asking again.
- Thorough: don't skip a decision-relevant angle, but organize answers so
  the client isn't drowning — lead with what matters most.
- NEVER ASSUME. If a message is ambiguous (unclear asset/pronoun, a slang
  term you're not confident about in context, a number with no stated
  unit/timeframe, an unspecified "tu opinión sobre qué"), stop and ask a
  precise clarifying question in Spanish before answering as if you'd
  resolved it yourself. Offer the 2-3 readings you're choosing between
  when that's useful so the client can just point at one.

Boundaries beyond the ones above:
- No directive financial instructions ("comprá ahora"). Lay out reasoning
  and tradeoffs; state your own read plainly when asked ("si me
  preguntás, yo creo que...") without dressing it up as a command.

You understand common Spanish-language crypto slang (ballena, rugpull,
hodlear, shitcoin, pump y dump, farmear, en verde/en rojo, lunear, manos
de diamante/de papel, quemar tokens, FOMO, FUD, DYOR, rekt, airdrop, gas,
CEX/DEX, TVL, degen, apalancamiento, liquidación, etc.) — but a term you
aren't confident about is exactly the kind of ambiguity you should ask
about, not silently reinterpret.

Beyond commentary on the live signal, you also teach: explain general
concepts on request (technical indicators, funding rates, order flow,
volatility regimes, position sizing, risk management) at whatever depth the
client wants, and explain the model's reasoning surface — which feature
families are populated vs. degraded, what that does to prediction quality,
and why the system would rather show UNAVAILABLE than guess. When asked for
performance-improvement advice, use the real model metrics and thresholds
given to you in [Model performance context] below — cite actual numbers,
never invent them. You can suggest specific, concrete changes (e.g. "raise
the BTC short threshold back toward the F1-optimal value if you want fewer,
higher-conviction signals") but you cannot apply them — say so, and point to
which script/config field would need editing. Be honest about uncertainty:
if confidence is low, a feature family is degraded, or a metric looks weak
(low precision), say so plainly rather than hedging vaguely.

You also occasionally generate automated signal notes on this same system
when a threshold fires (no client present for those) — if the client asks
about "your earlier note" on a signal, that's you; answer naturally rather
than acting like it was someone else."""


@app.get("/api/chat/status")
def chat_status():
    """
    Standardized discovery endpoint: reports which backend (if any) each
    chat surface currently resolves to, without exposing secrets. Lets an
    operator confirm what's actually wired without reading env vars/code.
    """
    def _probe_local_relay(endpoint: str) -> dict:
        """
        'configured' only means an env var is non-empty — it says nothing
        about whether the backend actually works (this was a real gap: an
        operator could see 'configured: true' while every real chat call
        was failing). For the local agent relay specifically (loopback
        address, its own /health endpoint reports whether the underlying
        agent binary genuinely exists), do a cheap real check rather than
        trusting config presence alone. Remote APIs (Pattern A) aren't
        probed here — a real reachability check for those means an actual
        paid/slow completion call, out of scope for a status endpoint.
        """
        if not (endpoint.startswith("http://127.0.0.1") or endpoint.startswith("http://localhost")):
            return {"verified": None, "verify_note": "remote backend — not probed, only presence-checked"}
        try:
            # Generous timeout: the relay's own /health does a real cached
            # test invocation of the underlying agent (not just a binary-
            # existence check), which can take a few seconds on a cache
            # miss — matching AGENT_RELAY_HEALTHCHECK_TIMEOUT_S's default.
            r = requests.get(f"{endpoint}/health", timeout=12)
            if r.status_code != 200:
                return {"verified": False, "verify_note": f"relay /health returned HTTP {r.status_code}"}
            h = r.json()
            if h.get("agent_binary_exists") is False:
                binary = h.get("agent_binary", "?")
                return {"verified": False, "verify_note": f"relay is up but agent binary {binary!r} not found on PATH"}
            if h.get("live_ok") is False:
                return {"verified": False, "verify_note": f"relay's real test call failed: {h.get('live_detail', 'unknown error')}"}
            if h.get("live_ok") is True:
                return {"verified": True, "verify_note": "relay reachable, real test invocation succeeded"}
            return {"verified": None, "verify_note": "relay reachable, live test result unavailable"}
        except Exception as e:
            return {"verified": False, "verify_note": f"relay unreachable: {e}"}

    def _describe() -> dict:
        cfg = _backend_config()
        override = cfg.get("backend_override")

        def _hermes_proxy_result():
            base = {"configured": True, "backend": "hermes_proxy", "endpoint": cfg["proxy_url"], "model": cfg["proxy_model"]}
            base.update(_probe_local_relay(cfg["proxy_url"]))
            return base

        def _anthropic_result():
            return {
                "configured": True, "backend": "anthropic", "endpoint": "https://api.anthropic.com/v1/messages",
                "model": cfg["anthropic_model"], "verified": None,
                "verify_note": "remote backend — not probed, only presence-checked",
            }

        def _ollama_result():
            base = {"configured": True, "backend": "ollama", "endpoint": cfg["ollama_url"], "model": cfg["ollama_model"]}
            base.update(_probe_local_relay(cfg["ollama_url"]))
            return base

        not_configured = {"configured": False, "backend": None, "endpoint": None, "model": None, "verified": False, "verify_note": "not configured"}

        if override:
            # CHAT_BACKEND names exactly one backend to use -- report on that
            # one specifically (including "misconfigured", if so) rather than
            # what would've been auto-detected.
            if override == "hermes_proxy":
                return _hermes_proxy_result() if cfg["proxy_url"] else {**not_configured, "verify_note": "CHAT_BACKEND=hermes_proxy but HERMES_PROXY_URL is unset"}
            if override == "anthropic":
                return _anthropic_result() if cfg["anthropic_key"] else {**not_configured, "verify_note": "CHAT_BACKEND=anthropic but ANTHROPIC_API_KEY is unset"}
            if override == "ollama":
                return _ollama_result() if cfg["ollama_url"] else {**not_configured, "verify_note": "CHAT_BACKEND=ollama but OLLAMA_URL is unset"}
            return {**not_configured, "verify_note": f"CHAT_BACKEND={override!r} not recognized (expected hermes_proxy|anthropic|ollama)"}

        # No override -- report whichever the auto-detect order would
        # actually pick (hermes_proxy, then anthropic, then ollama), matching
        # _call_llm_backend()'s real behavior exactly.
        if cfg["proxy_url"]:
            return _hermes_proxy_result()
        if cfg["anthropic_key"]:
            return _anthropic_result()
        if cfg["ollama_url"]:
            return _ollama_result()
        return not_configured

    # Was {"advisory_chat": ..., "tutor_chat": ...} before the two chat
    # personas were merged back into one 2026-07-23 — now just one surface.
    return {
        "chat": {"route": "/api/chat", **_describe()},
    }


# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send init payload for active symbol (default BTC)
        snaps = {sym: eng.snapshot() for sym, eng in engines.items()}
        await websocket.send_json({
            "type": "init",
            "assets": DISPLAY_ASSETS,
            "snapshots": {
                sym: {
                    "candles":             snap["candles"],
                    "prediction_long":     snap["latest_prediction_long"],
                    "prediction_short":    snap["latest_prediction_short"],
                    "signal":              snap["latest_signal"],
                    "position":            snap["position"],
                    "stats":               snap["stats"],
                    "latest_close":        snap["latest_close"],
                    "latest_atr":          snap["latest_atr"],
                } for sym, snap in snaps.items()
            }
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WS client error: {e}")
        manager.disconnect(websocket)

# ── Serve Dashboard ───────────────────────────────────────────────────────────
parent_dir = os.path.dirname(os.path.abspath(__file__))
dashboard_path = os.path.join(parent_dir, "dashboard")
if not os.path.exists(dashboard_path):
    dashboard_path = os.path.join(os.path.dirname(parent_dir), "dashboard")

if os.path.exists(dashboard_path):
    app.mount("/", StaticFiles(directory=dashboard_path, html=True), name="dashboard")
    logger.success(f"Mounted dashboard static files from {dashboard_path}")
else:
    logger.warning("Dashboard directory not found. Static files not mounted.")


if __name__ == "__main__":
    import uvicorn
    host = config["server"]["host"]
    port = config["server"]["port"]
    logger.info(f"Starting Predictor v2 on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
