#!/usr/bin/env python3
"""
prepare_full_dataset.py — Build a labeled training dataset for one primary pair.

Feature schema (matches July-2026 training report, 64 features):
  - Primary pair 15m:    base + structure + futures features
  - Multi-TF BTC:        1h / 4h / 1d  log-returns, EMAs, trend, ATR, volume, regime  (27)
  - Cross-asset context: 2 other pairs' returns, trend, volume (6)
  - Macro (daily):       Gold, Oil, DXY, SPX, VIX — returns, EMAs, trend, dir  (30)
  - Labels:              label_tp_before_sl_1h (long), label_short_tp_before_sl_1h (short)

Usage example (BTC as primary, ETH/SOL as context):
    python3 prepare_full_dataset.py \\
        --primary      BTC \\
        --primary-candles      data/raw/btc_15m.parquet \\
        --primary-mark         data/raw/btc_mark.parquet \\
        --primary-funding      data/raw/btc_funding.parquet \\
        --primary-1h           data/raw/btc_1h.parquet \\
        --primary-4h           data/raw/btc_4h.parquet \\
        --primary-1d           data/raw/btc_1d.parquet \\
        --ctx-a-candles        data/raw/eth_15m.parquet \\
        --ctx-a-mark           data/raw/eth_mark.parquet \\
        --ctx-a-funding        data/raw/eth_funding.parquet \\
        --ctx-b-candles        data/raw/sol_15m.parquet \\
        --ctx-b-mark           data/raw/sol_mark.parquet \\
        --ctx-b-funding        data/raw/sol_funding.parquet \\
        --macro-dir            data/macro \\
        --output               data/datasets/btc_full.parquet \\
        --tp-atr-mult 1.5 --sl-atr-mult 1.0 --horizon-bars 4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import _bootstrap  # noqa: F401  — adds src/ to sys.path

from lgbm_poc.features import build_feature_table
from lgbm_poc.futures import build_futures_market_frame, load_timeframe_frame
from lgbm_poc.io import write_parquet
from lgbm_poc.labels import label_tp_before_sl_1h, label_short_tp_before_sl_1h


# ── Multi-timeframe helpers ───────────────────────────────────────────────────

def _tf_features(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """
    Compute multi-timeframe BTC context features from a resampled OHLCV frame.
    Returns a narrow frame: [timestamp, prefix_*].
    """
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    close = out["close"].replace(0, np.nan)

    out[f"{prefix}_log_return_1"] = np.log(out["close"] / out["close"].shift(1).replace(0, np.nan)).fillna(0.0)
    out[f"{prefix}_log_return_3"] = np.log(out["close"] / out["close"].shift(3).replace(0, np.nan)).fillna(0.0)

    ema_fast = out["close"].ewm(span=9,  adjust=False).mean()
    ema_slow = out["close"].ewm(span=21, adjust=False).mean()
    out[f"{prefix}_ema_fast"]      = (ema_fast / close).fillna(1.0)
    out[f"{prefix}_ema_slow"]      = (ema_slow / close).fillna(1.0)
    out[f"{prefix}_trend_strength"] = ((ema_fast - ema_slow) / close).fillna(0.0)
    out[f"{prefix}_trend_dir"]     = (
        (ema_fast > ema_slow).astype(int) - (ema_fast < ema_slow).astype(int)
    )

    range_raw = out["high"] - out["low"]
    out[f"{prefix}_atr_pct"] = (range_raw.rolling(14, min_periods=3).mean() / close).fillna(0.0)

    vol_mean = out["volume"].rolling(20, min_periods=5).mean().fillna(1.0)
    vol_std  = out["volume"].rolling(20, min_periods=5).std().replace(0, np.nan)
    out[f"{prefix}_volume_zscore"] = ((out["volume"] - vol_mean) / vol_std).fillna(0.0)

    out[f"{prefix}_regime"] = 0
    trend = out[f"{prefix}_trend_strength"]
    out.loc[trend > 0.002,  f"{prefix}_regime"] =  1
    out.loc[trend < -0.002, f"{prefix}_regime"] = -1

    feat_cols = [c for c in out.columns if c.startswith(prefix + "_")]
    return out[["timestamp"] + feat_cols].copy()


def _load_tf_frame(path: str | None) -> pd.DataFrame | None:
    if not path or not Path(path).exists():
        return None
    df = load_timeframe_frame(path)
    return df


def _merge_asof_left(left: pd.DataFrame, right: pd.DataFrame, ts_col: str = "timestamp") -> pd.DataFrame:
    """Backward merge_asof — no lookahead."""
    return pd.merge_asof(
        left.sort_values(ts_col),
        right.sort_values(ts_col),
        on=ts_col,
        direction="backward",
    )


# ── Macro helpers ─────────────────────────────────────────────────────────────

MACRO_ASSETS = ["gold", "oil", "dxy", "spx", "vix"]
MACRO_FEATURE_COLS = [
    f"{asset}_{suffix}"
    for asset in MACRO_ASSETS
    for suffix in ("return_1d", "return_5d", "ema_fast", "ema_slow", "trend", "trend_dir")
]


def _load_macro(macro_dir: str | Path) -> pd.DataFrame | None:
    """
    Load all macro parquet files and merge into a single daily-index frame.
    Returns None if macro_dir doesn't exist or no files found.
    """
    macro_dir = Path(macro_dir)
    frames = {}
    for name in MACRO_ASSETS:
        path = macro_dir / f"{name}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.sort_values("timestamp").reset_index(drop=True)
            feat_cols = [c for c in df.columns if c.startswith(name + "_")]
            frames[name] = df[["timestamp"] + feat_cols]
        else:
            print(f"[WARN] Macro file not found: {path} — will zero-fill.")

    if not frames:
        return None

    merged = list(frames.values())[0]
    for df in list(frames.values())[1:]:
        merged = pd.merge_asof(
            merged.sort_values("timestamp"),
            df.sort_values("timestamp"),
            on="timestamp",
            direction="backward",
        )
    return merged


# ── Cross-asset context ───────────────────────────────────────────────────────

def _ctx_features(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """
    Extract stationary features from a context pair (not the primary).
    name: 'eth' | 'sol' | 'btc' etc.
    """
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    close = out["close"].replace(0, np.nan)

    out[f"{name}_return_1"] = np.log(out["close"] / out["close"].shift(1).replace(0, np.nan)).fillna(0.0)
    out[f"{name}_return_3"] = np.log(out["close"] / out["close"].shift(3).replace(0, np.nan)).fillna(0.0)
    ema_fast = out["close"].ewm(span=9,  adjust=False).mean()
    ema_slow = out["close"].ewm(span=21, adjust=False).mean()
    out[f"{name}_trend"] = ((ema_fast - ema_slow) / close).fillna(0.0)

    vol_mean = out["volume"].rolling(20, min_periods=5).mean().fillna(1.0)
    vol_std  = out["volume"].rolling(20, min_periods=5).std().replace(0, np.nan)
    vol_z    = ((out["volume"] - vol_mean) / vol_std).fillna(0.0)
    range_raw = out["high"] - out["low"]
    body_q   = ((out["close"] - out["open"]).abs() / range_raw.replace(0, np.nan)).fillna(0.0)
    body_q   = (body_q * (range_raw / close).fillna(0.0)).clip(lower=0.0)
    out[f"{name}_volume_block"] = (vol_z.clip(lower=0.0) * (1.0 + body_q)).fillna(0.0)

    feat_cols = [c for c in out.columns if c.startswith(name + "_")]
    return out[["timestamp"] + feat_cols]


def _load_ctx_bundle(candle_path: str | None, mark_path: str | None,
                     funding_path: str | None, rule: str) -> pd.DataFrame | None:
    if not candle_path or not Path(candle_path).exists():
        return None
    candles = load_timeframe_frame(candle_path)
    mark    = load_timeframe_frame(mark_path)    if mark_path    and Path(mark_path).exists()    else None
    funding = load_timeframe_frame(funding_path) if funding_path and Path(funding_path).exists() else None
    return build_futures_market_frame(candles, mark=mark, funding=funding, resample_rule=rule)


# ── Lower-TF micro-structure helpers ─────────────────────────────────────────

def _lower_tf_zero_cols(prefix: str) -> list[str]:
    return [
        f"{prefix}_bull_ratio",
        f"{prefix}_vol_tail_pct",
        f"{prefix}_max_body_ratio",
        f"{prefix}_trend",
        f"{prefix}_atr_ratio",
        f"{prefix}_volume_zscore",
    ]


def _lower_tf_features(df: pd.DataFrame, prefix: str, resample_rule: str = "15min") -> pd.DataFrame:
    """
    Aggregate 1m or 5m OHLCV into 15m-aligned features (no lookahead).

    Features:
      bull_ratio      — fraction of sub-candles that closed up (momentum quality)
      vol_tail_pct    — share of volume in last 20% of sub-candles (late-candle pressure)
      max_body_ratio  — strongest single sub-candle body/range (conviction proxy)
      trend           — normalised (ema_fast - ema_slow) / close on raw sub-candles
      atr_ratio       — sub-TF ATR / 15m close (intra-candle volatility level)
      volume_zscore   — rolling z-score of volume at sub-TF resolution, resampled to last value
    """
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")

    close  = out["close"].replace(0, np.nan)
    body   = (out["close"] - out["open"]).abs()
    range_ = (out["high"] - out["low"]).replace(0, np.nan)

    out["_bull"]        = (out["close"] >= out["open"]).astype(float)
    out["_body_ratio"]  = (body / range_).fillna(0.0)
    out["_atr"]         = range_.rolling(14, min_periods=3).mean().fillna(0.0)
    ema_fast = out["close"].ewm(span=9,  adjust=False).mean()
    ema_slow = out["close"].ewm(span=21, adjust=False).mean()
    out["_trend"]       = ((ema_fast - ema_slow) / close).fillna(0.0)
    vol_mean = out["volume"].rolling(20, min_periods=5).mean().fillna(1.0)
    vol_std  = out["volume"].rolling(20, min_periods=5).std().replace(0, pd.NA)
    out["_vol_z"]       = ((out["volume"] - vol_mean) / vol_std).fillna(0.0)

    out = out.set_index("timestamp")

    agg = pd.DataFrame()
    agg[f"{prefix}_bull_ratio"]     = out["_bull"].resample(resample_rule).mean()
    agg[f"{prefix}_max_body_ratio"] = out["_body_ratio"].resample(resample_rule).max()
    agg[f"{prefix}_trend"]          = out["_trend"].resample(resample_rule).last()
    agg[f"{prefix}_atr_ratio"]      = out["_atr"].resample(resample_rule).last()
    agg[f"{prefix}_volume_zscore"]  = out["_vol_z"].resample(resample_rule).last()

    # vol_tail_pct: share of volume in last 20% of bars within each 15m bucket
    def _tail_vol_pct(grp: pd.Series) -> float:
        if len(grp) == 0:
            return 0.0
        n_tail = max(1, len(grp) // 5)
        total  = grp.sum()
        return float(grp.iloc[-n_tail:].sum() / total) if total > 0 else 0.0

    agg[f"{prefix}_vol_tail_pct"] = out["volume"].resample(resample_rule).apply(_tail_vol_pct)

    agg = agg.fillna(0.0).reset_index().rename(columns={"timestamp": "timestamp"})
    agg["timestamp"] = pd.to_datetime(agg["timestamp"], utc=True, errors="coerce")
    return agg.sort_values("timestamp").reset_index(drop=True)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def build_full_dataset(args: argparse.Namespace) -> pd.DataFrame:
    rule = args.resample_rule

    # 1. Primary pair — base + structure + futures features
    print(f"[1/6] Loading primary pair ({args.primary}) 15m candles…")
    primary_raw = _load_ctx_bundle(args.primary_candles, args.primary_mark, args.primary_funding, rule)
    if primary_raw is None:
        raise FileNotFoundError(f"Primary candles not found: {args.primary_candles}")
    primary = build_feature_table(primary_raw)
    print(f"      {len(primary)} rows after feature build.")

    # 2a. Lower-TF micro-structure (1m / 5m → aggregated to 15m timestamps)
    print("[2/6] Adding lower-TF micro-structure (1m / 5m)…")
    for tf_label, path_attr, prefix in [
        ("1m", "primary_1m", "m1"),
        ("5m", "primary_5m", "m5"),
    ]:
        path = getattr(args, path_attr, None)
        tf_raw = _load_tf_frame(path)
        if tf_raw is not None:
            tf_feats = _lower_tf_features(tf_raw, prefix, resample_rule=rule)
            primary = _merge_asof_left(primary, tf_feats)
            print(f"      merged {prefix} ({len(tf_feats)} rows).")
        else:
            print(f"      [WARN] {tf_label} data not provided — zero-filling {prefix}_* columns.")
            for col in _lower_tf_zero_cols(prefix):
                primary[col] = 0.0

    # 2b. Higher-TF trend validation (1h / 4h / 1d)
    print("      Adding higher-TF trend context (1h / 4h / 1d)…")
    for tf_label, path_attr, prefix in [
        ("1h",  "primary_1h", "btc_1h"),
        ("4h",  "primary_4h", "btc_4h"),
        ("1d",  "primary_1d", "btc_1d"),
    ]:
        path = getattr(args, path_attr, None)
        tf_raw = _load_tf_frame(path)
        if tf_raw is not None:
            tf_feats = _tf_features(tf_raw, prefix)
            primary = _merge_asof_left(primary, tf_feats)
            print(f"      merged {prefix} ({len(tf_feats)} rows).")
        else:
            print(f"      [WARN] {tf_label} data not provided — zero-filling {prefix}_* columns.")
            for suffix in ("log_return_1", "log_return_3", "ema_fast", "ema_slow",
                           "trend_strength", "trend_dir", "atr_pct", "volume_zscore", "regime"):
                primary[f"{prefix}_{suffix}"] = 0.0

    # 3. Cross-asset context pairs (ctx-a and ctx-b)
    print("[3/6] Adding cross-asset context pairs…")
    for name, candle_attr, mark_attr, fund_attr in [
        ("eth", "ctx_a_candles", "ctx_a_mark", "ctx_a_funding"),
        ("sol", "ctx_b_candles", "ctx_b_mark", "ctx_b_funding"),
    ]:
        candle_path = getattr(args, candle_attr, None)
        mark_path   = getattr(args, mark_attr,   None)
        fund_path   = getattr(args, fund_attr,   None)
        ctx_raw = _load_ctx_bundle(candle_path, mark_path, fund_path, rule)
        if ctx_raw is not None:
            ctx_feats = _ctx_features(ctx_raw, name)
            primary = _merge_asof_left(primary, ctx_feats)
            print(f"      merged {name} ({len(ctx_feats)} rows).")
        else:
            print(f"      [WARN] {name} context not provided — zero-filling.")
            for suffix in ("return_1", "return_3", "trend", "volume_block"):
                primary[f"{name}_{suffix}"] = 0.0

    # 4. Macro features (daily, forward-filled onto 15m)
    print("[4/6] Merging macro features…")
    if args.macro_dir and Path(args.macro_dir).exists():
        macro = _load_macro(args.macro_dir)
        if macro is not None:
            primary = _merge_asof_left(primary, macro)
            print(f"      merged macro ({len(macro)} daily rows).")
        else:
            print("      [WARN] No macro parquet files found — zero-filling.")
            for col in MACRO_FEATURE_COLS:
                primary[col] = 0.0
    else:
        print(f"      [WARN] macro-dir {args.macro_dir!r} not found — zero-filling.")
        for col in MACRO_FEATURE_COLS:
            primary[col] = 0.0

    # Ensure all expected macro cols exist
    for col in MACRO_FEATURE_COLS:
        if col not in primary.columns:
            primary[col] = 0.0

    # 5. Label — compute long AND short in one pass
    print("[5/6] Labeling (long + short)…")
    labeled = label_tp_before_sl_1h(
        primary,
        horizon_bars=args.horizon_bars,
        tp_atr_mult=args.tp_atr_mult,
        sl_atr_mult=args.sl_atr_mult,
    )
    labeled = label_short_tp_before_sl_1h(
        labeled,
        horizon_bars=args.horizon_bars,
        tp_atr_mult=args.tp_atr_mult,
        sl_atr_mult=args.sl_atr_mult,
    )
    before = len(labeled)
    # Drop rows where EITHER label is NaN (both require the same horizon tail)
    labeled = labeled.dropna(subset=["label_tp_before_sl_1h", "label_short_tp_before_sl_1h"]).reset_index(drop=True)
    print(f"      {len(labeled)} labeled rows (dropped {before - len(labeled)} horizon tail).")

    for col, side in [("label_tp_before_sl_1h", "long"), ("label_short_tp_before_sl_1h", "short")]:
        pos = int(labeled[col].sum())
        neg = len(labeled) - pos
        print(f"      [{side}] {pos} positives ({100*pos/len(labeled):.1f}%), {neg} negatives.")

    return labeled


def main() -> int:
    parser = argparse.ArgumentParser(description="Build full labeled training dataset.")
    parser.add_argument("--primary",         required=True, help="Primary pair name, e.g. BTC")
    parser.add_argument("--primary-candles", required=True)
    parser.add_argument("--primary-mark",    default=None)
    parser.add_argument("--primary-funding", default=None)
    parser.add_argument("--primary-1m",      default=None, help="Primary pair 1m OHLCV parquet (micro-structure).")
    parser.add_argument("--primary-5m",      default=None, help="Primary pair 5m OHLCV parquet (micro-structure).")
    parser.add_argument("--primary-1h",      default=None, help="Primary pair 1h OHLCV parquet (trend validation).")
    parser.add_argument("--primary-4h",      default=None, help="Primary pair 4h OHLCV parquet.")
    parser.add_argument("--primary-1d",      default=None, help="Primary pair 1d OHLCV parquet.")
    parser.add_argument("--ctx-a-candles",   default=None, help="Context pair A 15m candles.")
    parser.add_argument("--ctx-a-mark",      default=None)
    parser.add_argument("--ctx-a-funding",   default=None)
    parser.add_argument("--ctx-b-candles",   default=None, help="Context pair B 15m candles.")
    parser.add_argument("--ctx-b-mark",      default=None)
    parser.add_argument("--ctx-b-funding",   default=None)
    parser.add_argument("--macro-dir",       default="data/macro")
    parser.add_argument("--output",          required=True)
    parser.add_argument("--resample-rule",   default="15min")
    parser.add_argument("--horizon-bars",    type=int,   default=4)
    parser.add_argument("--tp-atr-mult",     type=float, default=1.5)
    parser.add_argument("--sl-atr-mult",     type=float, default=1.0)
    args = parser.parse_args()

    df = build_full_dataset(args)

    out = Path(args.output)
    write_parquet(df, out)
    print(f"\n[6/6] Wrote {len(df)} rows → {out}")
    print(f"      Columns: {len(df.columns)}  |  Features ready for training.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
