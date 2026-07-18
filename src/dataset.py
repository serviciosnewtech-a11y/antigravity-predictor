from __future__ import annotations

from pathlib import Path
import pandas as pd


FEATURE_COLUMNS = [
    # Base BTC Features (41)
    "log_return_1",
    "log_return_3",
    "log_return_6",
    "range_1",
    "atr_proxy",
    "volatility_lookback",
    "hour_of_day",
    "day_of_week",
    "session_asia",
    "session_london",
    "session_newyork",
    "volume_zscore",
    "relative_volume",
    "volume_percentile",
    "body_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "atr_normalized_range",
    "stop_distance",
    "dist_ema_fast",
    "dist_ema_slow",
    "trend_strength",
    "trend_direction",
    "ema_slow_slope",
    "sweep_high_detected",
    "sweep_low_detected",
    "sweep_depth_atr",
    "sweep_rejection_ratio",
    "sweep_volume_zscore",
    "bullish_fvg_present",
    "bearish_fvg_present",
    "fvg_size_atr",
    "fvg_age_candles",
    "price_inside_fvg",
    "breakout_volume_confirmation",
    "rejection_volume_confirmation",
    "volume_block_strength",
    "atr_percentile",
    "range_compression",
    "high_volatility_flag",
    "market_regime",

    # Futures BTC Features (8)
    "funding_rate",
    "funding_rate_abs",
    "funding_rate_mean_4",
    "funding_rate_std_4",
    "mark_basis",
    "mark_premium",
    "mark_premium_mean_4",
    "futures_pressure",

    # Informative ETH Features (11)
    "eth_log_return_1",
    "eth_log_return_3",
    "eth_log_return_6",
    "eth_trend_strength",
    "eth_volume_block_strength",
    "eth_funding_rate",
    "eth_funding_rate_abs",
    "eth_funding_rate_mean_4",
    "eth_mark_basis",
    "eth_mark_premium",
    "eth_mark_premium_mean_4",

    # Informative SOL Features (11)
    "sol_log_return_1",
    "sol_log_return_3",
    "sol_log_return_6",
    "sol_trend_strength",
    "sol_volume_block_strength",
    "sol_funding_rate",
    "sol_funding_rate_abs",
    "sol_funding_rate_mean_4",
    "sol_mark_basis",
    "sol_mark_premium",
    "sol_mark_premium_mean_4",

    # Cross-Asset Spreads (10)
    "eth_close_rel_btc",
    "sol_close_rel_btc",
    "eth_return_spread",
    "sol_return_spread",
    "eth_trend_spread",
    "sol_trend_spread",
    "eth_funding_rate_spread",
    "sol_funding_rate_spread",
    "eth_mark_premium_spread",
    "sol_mark_premium_spread",
]

LABEL_COLUMN = "label_tp_before_sl_1h"


def load_dataset(path: str | Path) -> pd.DataFrame:
    return pd.read_parquet(Path(path))


def select_xy(df: pd.DataFrame, label_col: str = LABEL_COLUMN) -> tuple[pd.DataFrame, pd.Series]:
    clean = df.copy()
    missing = [column for column in FEATURE_COLUMNS if column not in clean.columns]
    for column in missing:
        clean[column] = 0.0
    clean = clean.dropna(subset=[label_col]).copy()
    clean[FEATURE_COLUMNS] = clean[FEATURE_COLUMNS].fillna(0.0)
    X = clean[FEATURE_COLUMNS]
    y = clean[label_col].astype(int)
    return X, y
