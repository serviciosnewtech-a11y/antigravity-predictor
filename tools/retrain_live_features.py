#!/usr/bin/env python3
"""
tools/retrain_live_features.py — H-13 remediation & causal HTF feature builder.

Updated to eliminate HTF lookahead leak by using available_at = timestamp + bar_duration.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import _bootstrap
from lgbm_poc.labels import label_tp_before_sl_1h, label_short_tp_before_sl_1h
from lgbm_poc.train import TrainConfig, train_binary_model
from download_ohlcv import fetch_ohlcv

from predictor_server import (
    build_features, FUNDING_FEATURE_COLUMNS, PEER_FEATURE_SUFFIXES,
    HTF_FEATURE_COLUMNS, HTF_TIMEFRAMES,
)

BASE_LIVE_FEATURE_COLUMNS = [
    "log_return_1", "log_return_3", "log_return_6", "range_1", "atr_proxy",
    "volatility_lookback", "hour_of_day", "day_of_week", "session_asia",
    "session_london", "session_newyork", "volume_zscore", "relative_volume",
    "volume_percentile", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "atr_normalized_range", "stop_distance", "dist_ema_fast", "dist_ema_slow",
    "trend_strength", "trend_direction", "ema_slow_slope",
    "sweep_high_detected", "sweep_low_detected", "sweep_depth_atr",
    "sweep_rejection_ratio", "sweep_volume_zscore", "bullish_fvg_present",
    "bearish_fvg_present", "fvg_size_atr", "fvg_age_candles",
    "price_inside_fvg", "breakout_volume_confirmation",
    "rejection_volume_confirmation", "volume_block_strength",
    "atr_percentile", "range_compression", "high_volatility_flag",
    "market_regime",
]
assert len(BASE_LIVE_FEATURE_COLUMNS) == 41
LIVE_FEATURE_COLUMNS = BASE_LIVE_FEATURE_COLUMNS

ASSETS = {
    "BTC/USDT": {"model_prefix": "btc", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
    "ETH/USDT": {"model_prefix": "eth", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
    "SOL/USDT": {"model_prefix": "sol", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
}
HORIZON_BARS = 4

BAR_DURATIONS = {
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
}


def peer_prefixes_for(symbol: str) -> list[str]:
    return [s.split("/")[0].lower() for s in ASSETS if s != symbol]


def feature_columns_for(symbol: str) -> list[str]:
    peer_cols = [f"{p}_{suf}" for p in peer_prefixes_for(symbol) for suf in PEER_FEATURE_SUFFIXES]
    return BASE_LIVE_FEATURE_COLUMNS + FUNDING_FEATURE_COLUMNS + peer_cols + HTF_FEATURE_COLUMNS


def compute_htf_series(kline: pd.DataFrame, tf_name: str) -> pd.DataFrame:
    df = kline.sort_values("timestamp").reset_index(drop=True)
    closes, highs, lows = df["close"], df["high"], df["low"]
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

    duration = BAR_DURATIONS[tf_name]
    available_at = pd.to_datetime(df["timestamp"]) + duration

    return pd.DataFrame({
        "timestamp": df["timestamp"],
        "available_at": available_at,
        f"btc_{tf_name}_trend_direction": trend_direction,
        f"btc_{tf_name}_trend_strength": trend_strength,
        f"btc_{tf_name}_return_3": return_3,
        f"btc_{tf_name}_atr_percentile": atr_percentile,
    })


def build_htf_table(since_ms: int, cache_dir: Path, budget_s_per_tf: float = 20.0) -> Optional[dict]:
    tables = {}
    for tf_name, interval in HTF_TIMEFRAMES:
        kline = cached_fetch_kline("BTC/USDT", interval, since_ms, cache_dir, budget_s=budget_s_per_tf)
        if kline.empty:
            return None
        tables[tf_name] = compute_htf_series(kline, tf_name)
    return tables


def merge_htf_onto_grid(ohlcv_timestamps: pd.Series, htf_tables: dict) -> pd.DataFrame:
    grid = pd.DataFrame({"timestamp": pd.to_datetime(ohlcv_timestamps).dt.tz_localize(None)}).sort_values("timestamp")

    for tf_name in ["1h", "4h"]:
        if tf_name in htf_tables:
            htf = htf_tables[tf_name].copy()
            htf["available_at"] = pd.to_datetime(htf["available_at"]).dt.tz_localize(None)
            cols = [c for c in htf.columns if c not in ("timestamp", "available_at")]
            grid = pd.merge_asof(
                grid,
                htf[["available_at"] + cols].sort_values("available_at"),
                left_on="timestamp", right_on="available_at",
                direction="backward"
            )
            if "available_at" in grid.columns:
                grid = grid.drop(columns=["available_at"])

    for col in HTF_FEATURE_COLUMNS:
        if col not in grid.columns:
            grid[col] = 0.5 if "atr_percentile" in col else 0.0
        else:
            grid[col] = grid[col].fillna(0.0 if "atr_percentile" not in col else 0.5)

    return grid[["timestamp"] + HTF_FEATURE_COLUMNS]
