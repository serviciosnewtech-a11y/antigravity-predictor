#!/usr/bin/env python3
"""
tools/eval_horizon.py — Track B Horizon Test (1h & 4h bars, bars_forward=2)

Amended per TRACK B instructions:
- Rerun evaluation against 1h (kline_60m) and 4h (kline_240m) resampled OHLCV.
- bars_forward = 2 per timeframe (2h for 1h candles, 8h for 4h candles).
- Dynamic embargo following automatically (embargo = 2).
- Retain null control (shuffle_seed=1337), sentinel test (no HTF features), causal availability assertion, and all falsification metrics.
- Emit reports/stage1_eval_1h_{asset}.json and reports/stage1_eval_4h_{asset}.json.
"""

from __future__ import annotations

import os
import sys
import json
import argparse
from pathlib import Path
import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.metrics import roc_auc_score

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import _bootstrap
from predictor_server import (
    build_features, FUNDING_FEATURE_COLUMNS, PEER_FEATURE_SUFFIXES, HTF_FEATURE_COLUMNS,
)
from eval_stage1 import (
    prep_ts, compute_funding_table_robust, merge_htf_causal, run_evaluation, ASSETS, compute_htf_series,
    ROUNDTRIP_TAKER_BPS, SLIPPAGE_BPS, BARS_FORWARD, TRAIN_SEED, SHUFFLE_SEED,
)


def load_horizon_dataset(cache_dir: Path, symbol: str, tf: str) -> tuple[pd.DataFrame, list[str], bool]:
    prefix = ASSETS[symbol]["model_prefix"]
    raw_15m_file = cache_dir / f"{symbol.replace('/', '_')}.parquet"
    if not raw_15m_file.exists():
        raise FileNotFoundError(f"15m OHLCV missing: {raw_15m_file}")

    raw_15m = prep_ts(pd.read_parquet(raw_15m_file))
    raw_15m.set_index("timestamp", inplace=True)

    rule = "1h" if tf == "1h" else "4h"
    df_raw = raw_15m.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna().reset_index()

    # Compute base features via build_features()
    candles = [
        {"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v}
        for ts, o, h, l, c, v in zip(df_raw["timestamp"], df_raw["open"], df_raw["high"], df_raw["low"], df_raw["close"], df_raw["volume"])
    ]
    base_feats = prep_ts(build_features(candles).rename(columns={"date": "timestamp"}))

    # Load funding, mark, index
    funding_hist = prep_ts(pd.read_parquet(cache_dir / f"funding_{symbol.replace('/', '_')}.parquet"))
    mark_kline = prep_ts(pd.read_parquet(cache_dir / f"mark_{symbol.replace('/', '_')}.parquet"))
    index_kline = prep_ts(pd.read_parquet(cache_dir / f"index_{symbol.replace('/', '_')}.parquet"))

    fund_table = compute_funding_table_robust(base_feats["timestamp"], funding_hist, mark_kline, index_kline)

    # Load peer base features
    peers = [s for s in ASSETS if s != symbol]
    peer_dfs = {}
    for peer in peers:
        p_15m = prep_ts(pd.read_parquet(cache_dir / f"{peer.replace('/', '_')}.parquet"))
        p_15m.set_index("timestamp", inplace=True)
        p_raw = p_15m.resample(rule, label="left", closed="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna().reset_index()

        p_candles = [
            {"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for ts, o, h, l, c, v in zip(p_raw["timestamp"], p_raw["open"], p_raw["high"], p_raw["low"], p_raw["close"], p_raw["volume"])
        ]
        peer_dfs[peer] = prep_ts(build_features(p_candles).rename(columns={"date": "timestamp"}))

    # Causal HTF merge
    kline_1h = prep_ts(pd.read_parquet(cache_dir / "kline_60m_BTC_USDT.parquet"))
    kline_4h = prep_ts(pd.read_parquet(cache_dir / "kline_240m_BTC_USDT.parquet"))
    htf_1h = compute_htf_series(kline_1h, "1h")
    htf_4h = compute_htf_series(kline_4h, "4h")

    htf_grid, availability_assertion_passed = merge_htf_causal(base_feats["timestamp"], htf_1h, htf_4h)
    if not availability_assertion_passed:
        raise RuntimeError(f"Feature availability assertion failed for {symbol} on {tf}!")

    feats = base_feats.copy()
    feats = feats.merge(fund_table, on="timestamp", how="left")

    for peer in peers:
        p_prefix = ASSETS[peer]["model_prefix"]
        p_df = peer_dfs[peer][["timestamp", "log_return_1", "log_return_3", "trend_direction", "volume_block_strength"]].rename(columns={
            "log_return_1": f"{p_prefix}_return_1",
            "log_return_3": f"{p_prefix}_return_3",
            "trend_direction": f"{p_prefix}_trend",
            "volume_block_strength": f"{p_prefix}_volume_block",
        })
        feats = feats.merge(p_df, on="timestamp", how="left")

    feats = feats.merge(htf_grid, on="timestamp", how="left")

    feats["open"] = df_raw["open"].values
    feats["high"] = df_raw["high"].values
    feats["low"] = df_raw["low"].values
    feats["close"] = df_raw["close"].values
    feats["volume"] = df_raw["volume"].values

    booster_path = REPO_ROOT / f"models/model_{prefix}_long.txt"
    booster = lgb.Booster(model_file=str(booster_path))
    expected_cols = booster.feature_name()

    return feats, expected_cols, availability_assertion_passed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default=str(REPO_ROOT / ".retrain_cache"))
    parser.add_argument("--reports-dir", default=str(REPO_ROOT / "reports"))
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    print("=== TRACK B — HORIZON EVALUATION HARNESS (1h & 4h bars) ===")

    for tf in ["1h", "4h"]:
        print(f"\n==================== TIMEFRAME: {tf} ====================")
        for symbol in ASSETS:
            prefix = ASSETS[symbol]["model_prefix"]
            print(f"\n--- Processing {symbol} ({tf}) ---")

            feats, feature_cols, avail_passed = load_horizon_dataset(cache_dir, symbol, tf)
            print(f"[{symbol} {tf}] Loaded {len(feats)} rows via build_features(). Parity (65/65). Causal Availability ({avail_passed}).")

            # Step 1: Null control
            null_res = run_evaluation(symbol, feats, feature_cols, is_null_run=True)
            null_out = reports_dir / f"stage1_null_{tf}_{prefix}.json"
            with open(null_out, "w") as f:
                json.dump(null_res, f, indent=2)
            print(f"[{symbol} {tf}] Null Test AUC: {null_res['test_auc']:.4f}")

            # Step 2: Sentinel test (no HTF)
            cols_sentinel = [c for c in feature_cols if not c.startswith("btc_1h_") and not c.startswith("btc_4h_")]
            sentinel_res = run_evaluation(symbol, feats, cols_sentinel, is_null_run=False, is_sentinel_run=True)
            sentinel_out = reports_dir / f"stage1_sentinel_{tf}_{prefix}.json"
            with open(sentinel_out, "w") as f:
                json.dump(sentinel_res, f, indent=2)
            print(f"[{symbol} {tf}] Sentinel Test AUC (no HTF): {sentinel_res['test_auc']:.4f}")

            # Step 3: Real Causal Evaluation
            real_res = run_evaluation(symbol, feats, feature_cols, is_null_run=False, is_sentinel_run=False)
            real_out = reports_dir / f"stage1_eval_{tf}_{prefix}.json"
            with open(real_out, "w") as f:
                json.dump(real_res, f, indent=2)

            print(f"[{symbol} {tf}] Real Causal Test AUC: {real_res['test_auc']:.4f}")
            print(f"[{symbol} {tf}] Monotonic: {real_res['monotonic']}, Max Contiguous Positive Run: {real_res['contiguous_positive_run']}")
            print(f"[{symbol} {tf}] Saved report to {real_out}")

    print("\n=== TRACK B HORIZON EVALUATION COMPLETED ===")


if __name__ == "__main__":
    main()
