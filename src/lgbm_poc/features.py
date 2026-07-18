from __future__ import annotations

import numpy as np
import pandas as pd

from lgbm_poc.baseline import BASELINE


def _utc_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce")


def add_basic_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = out.sort_values("timestamp").reset_index(drop=True)
    out["timestamp"] = _utc_datetime(out["timestamp"])

    # Log returns (stationary returns)
    close_val = out["close"].replace(0, pd.NA)
    out["log_return_1"] = np.log(out["close"] / out["close"].shift(1).replace(0, pd.NA)).fillna(0.0)
    out["log_return_3"] = np.log(out["close"] / out["close"].shift(3).replace(0, pd.NA)).fillna(0.0)
    out["log_return_6"] = np.log(out["close"] / out["close"].shift(6).replace(0, pd.NA)).fillna(0.0)

    # Normalized range and body features
    range_raw = out["high"] - out["low"]
    out["range_1"] = (range_raw / close_val).fillna(0.0)
    out["body"] = (out["close"] - out["open"]).abs()

    # ATR proxy
    out["atr_proxy"] = range_raw.rolling(14, min_periods=5).mean().fillna(0.0)

    # Volatility
    out["volatility_lookback"] = out["log_return_1"].rolling(20, min_periods=5).std().fillna(0.0)

    # Time features
    out["hour_of_day"] = out["timestamp"].dt.hour.fillna(0).astype(int)
    out["day_of_week"] = out["timestamp"].dt.dayofweek.fillna(0).astype(int)
    out["session_asia"] = out["hour_of_day"].between(0, 7).astype(int)
    out["session_london"] = out["hour_of_day"].between(7, 13).astype(int)
    out["session_newyork"] = out["hour_of_day"].between(13, 21).astype(int)

    # Volume features
    out["vol_mean_20"] = out["volume"].rolling(20, min_periods=5).mean().fillna(1.0)
    out["vol_std_20"] = out["volume"].rolling(20, min_periods=5).std().fillna(0.0)
    out["volume_zscore"] = ((out["volume"] - out["vol_mean_20"]) / out["vol_std_20"].replace(0, pd.NA)).fillna(0.0)
    out["relative_volume"] = (out["volume"] / out["vol_mean_20"]).fillna(1.0)

    vol_min = out["volume"].rolling(100, min_periods=10).min()
    vol_max = out["volume"].rolling(100, min_periods=10).max()
    out["volume_percentile"] = ((out["volume"] - vol_min) / (vol_max - vol_min).replace(0, pd.NA)).fillna(0.5)

    # Wick and body ratios
    range_safe = range_raw.replace(0, pd.NA)
    out["body_ratio"] = (out["body"] / range_safe).fillna(0.0)

    close_open_max = out[["close", "open"]].max(axis=1)
    close_open_min = out[["close", "open"]].min(axis=1)
    out["upper_wick_ratio"] = ((out["high"] - close_open_max) / range_safe).fillna(0.0)
    out["lower_wick_ratio"] = ((close_open_min - out["low"]) / range_safe).fillna(0.0)

    out["atr_normalized_range"] = (range_raw / out["atr_proxy"].replace(0, pd.NA)).fillna(0.0)
    out["stop_distance"] = (out["atr_proxy"] / close_val).fillna(0.0)

    # EMAs and distances
    out["ema_fast"] = out["close"].ewm(span=9, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=21, adjust=False).mean()

    out["dist_ema_fast"] = ((out["close"] - out["ema_fast"]) / close_val).fillna(0.0)
    out["dist_ema_slow"] = ((out["close"] - out["ema_slow"]) / close_val).fillna(0.0)
    out["trend_strength"] = ((out["ema_fast"] - out["ema_slow"]) / close_val).fillna(0.0)
    out["trend_direction"] = (out["ema_fast"] > out["ema_slow"]).astype(int) - (out["ema_fast"] < out["ema_slow"]).astype(int)

    # Slow EMA slope (stationary)
    out["ema_slow_slope"] = ((out["ema_slow"] - out["ema_slow"].shift(1)) / out["ema_slow"].replace(0, pd.NA)).fillna(0.0)

    return out


def add_structure_features(
    df: pd.DataFrame,
    sweep_lookback: int = BASELINE.sweep_lookback,
    volume_block_window: int = BASELINE.volume_block_window,
) -> pd.DataFrame:
    out = df.copy()
    atr_safe = out["atr_proxy"].replace(0, pd.NA)

    # Prior extremes (shifted by 1 to prevent leak)
    prev_high = out["high"].shift(1).rolling(sweep_lookback, min_periods=5).max()
    prev_low = out["low"].shift(1).rolling(sweep_lookback, min_periods=5).min()

    # 1. Stricter Liquidity Sweeps
    out["sweep_high_detected"] = ((out["high"] > prev_high) & (out["close"] < prev_high)).astype(int)
    out["sweep_low_detected"] = ((out["low"] < prev_low) & (out["close"] > prev_low)).astype(int)

    out["sweep_depth_atr"] = (
        ((out["high"] - prev_high) * out["sweep_high_detected"] + (prev_low - out["low"]) * out["sweep_low_detected"])
        / atr_safe
    ).fillna(0.0)

    out["sweep_rejection_ratio"] = (
        out["sweep_high_detected"] * out["upper_wick_ratio"] + out["sweep_low_detected"] * out["lower_wick_ratio"]
    )

    out["sweep_volume_zscore"] = ((out["sweep_high_detected"] + out["sweep_low_detected"]) * out["volume_zscore"])

    # 2. Fair Value Gaps (FVGs)
    high_2 = out["high"].shift(2)
    low_2 = out["low"].shift(2)
    out["bullish_fvg_present"] = (out["low"] > high_2).astype(int)
    out["bearish_fvg_present"] = (out["high"] < low_2).astype(int)

    out["fvg_size_atr"] = (
        ((out["low"] - high_2) * out["bullish_fvg_present"] + (low_2 - out["high"]) * out["bearish_fvg_present"])
        / atr_safe
    ).fillna(0.0)

    # FVG Age and Fill (calculated cleanly in vector format using forward fill)
    fvg_event = (out["bullish_fvg_present"] | out["bearish_fvg_present"])
    fvg_group = fvg_event.cumsum()
    out["fvg_age_candles"] = out.groupby(fvg_group).cumcount()
    # If no FVG has occurred yet, set age to a large number
    out.loc[fvg_group == 0, "fvg_age_candles"] = 999.0

    # Price inside active FVG check (uses FVG of previous candle)
    prev_bullish_fvg = out["bullish_fvg_present"].shift(1).fillna(0).astype(bool)
    prev_bearish_fvg = out["bearish_fvg_present"].shift(1).fillna(0).astype(bool)

    out["price_inside_fvg"] = 0
    out.loc[prev_bullish_fvg & (out["close"] < out["low"].shift(1)) & (out["close"] > high_2.shift(1)), "price_inside_fvg"] = 1
    out.loc[prev_bearish_fvg & (out["close"] > out["high"].shift(1)) & (out["close"] < low_2.shift(1)), "price_inside_fvg"] = -1

    # 3. Volume Confirmations
    out["breakout_volume_confirmation"] = (out["volume_zscore"] * (out["close"] > out["close"].shift(1)).astype(int)).clip(lower=0.0).fillna(0.0)
    out["rejection_volume_confirmation"] = (out["volume_zscore"] * (out["upper_wick_ratio"] + out["lower_wick_ratio"])).clip(lower=0.0).fillna(0.0)

    # 4. Volume block strength
    vol_mean = out["volume"].rolling(volume_block_window, min_periods=5).mean()
    vol_std = out["volume"].rolling(volume_block_window, min_periods=5).std().replace(0, pd.NA)
    vol_z = ((out["volume"] - vol_mean) / vol_std).fillna(0.0)
    body_quality = (out["body_ratio"].fillna(0.0) * out["range_1"].fillna(0.0)).clip(lower=0.0)
    out["volume_block_strength"] = (vol_z.clip(lower=0.0) * (1.0 + body_quality)).fillna(0.0)

    atr_min = out["atr_proxy"].rolling(100, min_periods=10).min()
    atr_max = out["atr_proxy"].rolling(100, min_periods=10).max()
    out["atr_percentile"] = ((out["atr_proxy"] - atr_min) / (atr_max - atr_min).replace(0, pd.NA)).fillna(0.5)

    rolling_max_20 = out["high"].rolling(20).max()
    rolling_min_20 = out["low"].rolling(20).min()
    out["range_compression"] = (out["atr_proxy"] / (rolling_max_20 - rolling_min_20).replace(0, pd.NA)).fillna(0.0)

    out["high_volatility_flag"] = (out["atr_proxy"] > out["atr_proxy"].rolling(50).median().fillna(0.0)).astype(int)

    out["market_regime"] = 0
    out.loc[(out["trend_strength"] > 0.002) & (out["volatility_lookback"] > 0), "market_regime"] = 1
    out.loc[(out["trend_strength"] < -0.002) & (out["volatility_lookback"] > 0), "market_regime"] = -1

    return out


def add_futures_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close_val = out["close"].replace(0, pd.NA)
    if "mark_close" not in out.columns:
        out["mark_close"] = out["close"]
    if "funding_rate" not in out.columns:
        out["funding_rate"] = 0.0

    # Normalized futures basis and premium
    out["mark_basis"] = ((out["mark_close"] - out["close"]) / close_val).fillna(0.0)
    out["mark_premium"] = out["mark_basis"]
    out["mark_premium_mean_4"] = out["mark_premium"].rolling(4, min_periods=1).mean().fillna(0.0)
    out["funding_rate_abs"] = out["funding_rate"].abs().fillna(0.0)
    out["funding_rate_mean_4"] = out["funding_rate"].rolling(4, min_periods=1).mean().fillna(0.0)
    out["funding_rate_std_4"] = out["funding_rate"].rolling(4, min_periods=1).std().fillna(0.0)
    out["futures_pressure"] = (out["mark_premium"] - out["funding_rate"]).fillna(0.0)
    return out


def build_feature_table(df: pd.DataFrame) -> pd.DataFrame:
    out = add_basic_features(df)
    out = add_structure_features(out)
    out = add_futures_features(out)
    return out.dropna().reset_index(drop=True)


def build_cross_asset_context(
    btc: pd.DataFrame,
    eth: pd.DataFrame,
    sol: pd.DataFrame,
) -> pd.DataFrame:
    btc_feat = build_feature_table(btc).copy()
    eth_feat = build_feature_table(eth).copy()
    sol_feat = build_feature_table(sol).copy()

    # Columns to extract from ETH and SOL (ensuring no absolute prices or raw wicks/ranges remain)
    base_cols = [
        "timestamp",
        "log_return_1",
        "log_return_3",
        "log_return_6",
        "trend_strength",
        "volume_block_strength",
        "close",  # Needed temporarily to compute the spreads, will be dropped
        "funding_rate",
        "funding_rate_abs",
        "funding_rate_mean_4",
        "mark_basis",
        "mark_premium",
        "mark_premium_mean_4",
    ]
    eth_cols = ["timestamp", *base_cols[1:]]
    sol_cols = ["timestamp", *base_cols[1:]]

    eth_ctx = eth_feat[eth_cols].rename(
        columns={
            "log_return_1": "eth_log_return_1",
            "log_return_3": "eth_log_return_3",
            "log_return_6": "eth_log_return_6",
            "trend_strength": "eth_trend_strength",
            "volume_block_strength": "eth_volume_block_strength",
            "close": "eth_close",
            "funding_rate": "eth_funding_rate",
            "funding_rate_abs": "eth_funding_rate_abs",
            "funding_rate_mean_4": "eth_funding_rate_mean_4",
            "mark_basis": "eth_mark_basis",
            "mark_premium": "eth_mark_premium",
            "mark_premium_mean_4": "eth_mark_premium_mean_4",
        }
    )
    sol_ctx = sol_feat[sol_cols].rename(
        columns={
            "log_return_1": "sol_log_return_1",
            "log_return_3": "sol_log_return_3",
            "log_return_6": "sol_log_return_6",
            "trend_strength": "sol_trend_strength",
            "volume_block_strength": "sol_volume_block_strength",
            "close": "sol_close",
            "funding_rate": "sol_funding_rate",
            "funding_rate_abs": "sol_funding_rate_abs",
            "funding_rate_mean_4": "sol_funding_rate_mean_4",
            "mark_basis": "sol_mark_basis",
            "mark_premium": "sol_mark_premium",
            "mark_premium_mean_4": "sol_mark_premium_mean_4",
        }
    )

    out = btc_feat.merge(eth_ctx, on="timestamp", how="inner").merge(sol_ctx, on="timestamp", how="inner")

    # Calculate stationary cross-asset spread features
    out["eth_close_rel_btc"] = (out["eth_close"] / out["close"].replace(0, pd.NA)).fillna(1.0) - 1.0
    out["sol_close_rel_btc"] = (out["sol_close"] / out["close"].replace(0, pd.NA)).fillna(1.0) - 1.0
    out["eth_return_spread"] = out["eth_log_return_1"] - out["log_return_1"]
    out["sol_return_spread"] = out["sol_log_return_1"] - out["log_return_1"]
    out["eth_trend_spread"] = out["eth_trend_strength"] - out["trend_strength"]
    out["sol_trend_spread"] = out["sol_trend_strength"] - out["trend_strength"]
    out["eth_funding_rate_spread"] = out["eth_funding_rate"] - out["funding_rate"]
    out["sol_funding_rate_spread"] = out["sol_funding_rate"] - out["funding_rate"]
    out["eth_mark_premium_spread"] = out["eth_mark_premium"] - out["mark_premium"]
    out["sol_mark_premium_spread"] = out["sol_mark_premium"] - out["mark_premium"]

    # Drop absolute close prices for ETH/SOL
    out = out.drop(columns=["eth_close", "sol_close"])
    return out.dropna().reset_index(drop=True)
