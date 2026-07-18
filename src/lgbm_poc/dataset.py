from __future__ import annotations

from pathlib import Path
import pandas as pd


FEATURE_COLUMNS = [
    # ── Primary pair features (49) ────────────────────────────────────────────

    # Basic (24)
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

    # Structure / SMC (17)
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

    # Futures (8)
    "funding_rate",
    "funding_rate_abs",
    "funding_rate_mean_4",
    "funding_rate_std_4",
    "mark_basis",
    "mark_premium",
    "mark_premium_mean_4",
    "futures_pressure",

    # ── Micro-structure — 1m aggregated to 15m (6) ───────────────────────────
    "m1_bull_ratio",
    "m1_vol_tail_pct",
    "m1_max_body_ratio",
    "m1_trend",
    "m1_atr_ratio",
    "m1_volume_zscore",

    # ── Micro-structure — 5m aggregated to 15m (6) ───────────────────────────
    "m5_bull_ratio",
    "m5_vol_tail_pct",
    "m5_max_body_ratio",
    "m5_trend",
    "m5_atr_ratio",
    "m5_volume_zscore",

    # ── Higher-TF trend context — 1h (9) ─────────────────────────────────────
    "btc_1h_log_return_1",
    "btc_1h_log_return_3",
    "btc_1h_ema_fast",
    "btc_1h_ema_slow",
    "btc_1h_trend_strength",
    "btc_1h_trend_dir",
    "btc_1h_atr_pct",
    "btc_1h_volume_zscore",
    "btc_1h_regime",

    # ── Higher-TF trend context — 4h (9) ─────────────────────────────────────
    "btc_4h_log_return_1",
    "btc_4h_log_return_3",
    "btc_4h_ema_fast",
    "btc_4h_ema_slow",
    "btc_4h_trend_strength",
    "btc_4h_trend_dir",
    "btc_4h_atr_pct",
    "btc_4h_volume_zscore",
    "btc_4h_regime",

    # ── Higher-TF trend context — 1d (9) ─────────────────────────────────────
    "btc_1d_log_return_1",
    "btc_1d_log_return_3",
    "btc_1d_ema_fast",
    "btc_1d_ema_slow",
    "btc_1d_trend_strength",
    "btc_1d_trend_dir",
    "btc_1d_atr_pct",
    "btc_1d_volume_zscore",
    "btc_1d_regime",

    # ── Cross-asset context (8) ───────────────────────────────────────────────
    "eth_return_1",
    "eth_return_3",
    "eth_trend",
    "eth_volume_block",
    "sol_return_1",
    "sol_return_3",
    "sol_trend",
    "sol_volume_block",

    # ── Macro — Gold (6) ─────────────────────────────────────────────────────
    "gold_return_1d",
    "gold_return_5d",
    "gold_ema_fast",
    "gold_ema_slow",
    "gold_trend",
    "gold_trend_dir",

    # ── Macro — Oil (6) ──────────────────────────────────────────────────────
    "oil_return_1d",
    "oil_return_5d",
    "oil_ema_fast",
    "oil_ema_slow",
    "oil_trend",
    "oil_trend_dir",

    # ── Macro — DXY (6) ──────────────────────────────────────────────────────
    "dxy_return_1d",
    "dxy_return_5d",
    "dxy_ema_fast",
    "dxy_ema_slow",
    "dxy_trend",
    "dxy_trend_dir",

    # ── Macro — SPX (6) ──────────────────────────────────────────────────────
    "spx_return_1d",
    "spx_return_5d",
    "spx_ema_fast",
    "spx_ema_slow",
    "spx_trend",
    "spx_trend_dir",

    # ── Macro — VIX (6) ──────────────────────────────────────────────────────
    "vix_return_1d",
    "vix_return_5d",
    "vix_ema_fast",
    "vix_ema_slow",
    "vix_trend",
    "vix_trend_dir",
]
# Total: 49 primary + 12 micro + 27 higher-TF + 8 cross-asset + 30 macro = 126

LABEL_COLUMN = "label_tp_before_sl_1h"


def load_dataset(path: str | Path) -> pd.DataFrame:
    return pd.read_parquet(Path(path))


def select_xy(df: pd.DataFrame, label_col: str = LABEL_COLUMN) -> tuple[pd.DataFrame, pd.Series]:
    clean = df.copy()
    missing = [col for col in FEATURE_COLUMNS if col not in clean.columns]
    for col in missing:
        clean[col] = 0.0
    clean = clean.dropna(subset=[label_col]).copy()
    clean[FEATURE_COLUMNS] = clean[FEATURE_COLUMNS].fillna(0.0)
    X = clean[FEATURE_COLUMNS]
    y = clean[label_col].astype(int)
    return X, y
