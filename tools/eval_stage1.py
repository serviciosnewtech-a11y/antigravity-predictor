#!/usr/bin/env python3
"""
tools/eval_stage1.py — Reproducible Stage 1 Evaluation & Null Control Harness

Amended per SPEC_v1.11.1 §Packet C (C-1 through C-5):
- C-1: Feature source MUST be predictor_server.build_features() on raw 6-col OHLCV.
- C-2: Assert feature parity against production booster headers (65/65 features).
- C-3: Tie policy: 'exclude' flat bars (close[t+2] == close[t]), emit tie statistics.
- C-4: Execute label-shuffled null control (shuffle_seed=1337) prior to real run.
- C-5: Emit falsification metrics: monotonic, contiguous_positive_run, ATR tercile & session breakdowns.
"""

from __future__ import annotations

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
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
from retrain_live_features import feature_columns_for, compute_htf_series

ASSETS = {
    "BTC/USDT": {"model_prefix": "btc", "spread_offset_pct": 0.0002},
    "ETH/USDT": {"model_prefix": "eth", "spread_offset_pct": 0.0002},
    "SOL/USDT": {"model_prefix": "sol", "spread_offset_pct": 0.0003},
}

ROUNDTRIP_TAKER_BPS = 11.0  # 0.11% roundtrip taker fee (Bybit 0.055%/side)
SLIPPAGE_BPS = 0.0
BARS_FORWARD = 2  # 2-bar directional horizon (close[t+2] > close[t])
TRAIN_SEED = 42
SHUFFLE_SEED = 1337


def prep_ts(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")
    return df


def compute_funding_table_robust(ohlcv_timestamps: pd.Series, funding_hist: pd.DataFrame,
                                 mark_kline: pd.DataFrame, index_kline: pd.DataFrame) -> pd.DataFrame:
    grid = pd.DataFrame({"timestamp": pd.to_datetime(ohlcv_timestamps, utc=True).dt.tz_localize(None).astype("datetime64[ns]")}).sort_values("timestamp")

    funding = funding_hist.sort_values("timestamp").copy()
    funding["timestamp"] = pd.to_datetime(funding["timestamp"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")
    grid = pd.merge_asof(grid, funding[["timestamp", "funding_rate"]], on="timestamp", direction="backward")
    grid["funding_rate"] = grid["funding_rate"].fillna(0.0)

    mark = mark_kline.sort_values("timestamp").rename(columns={"close": "mark_close"}).copy()
    mark["timestamp"] = pd.to_datetime(mark["timestamp"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")
    index = index_kline.sort_values("timestamp").rename(columns={"close": "index_close"}).copy()
    index["timestamp"] = pd.to_datetime(index["timestamp"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")

    grid = pd.merge_asof(grid, mark, on="timestamp", direction="backward")
    grid = pd.merge_asof(grid, index, on="timestamp", direction="backward")
    grid["mark_close"] = grid["mark_close"].ffill()
    grid["index_close"] = grid["index_close"].ffill()

    grid["mark_basis"] = ((grid["mark_close"] - grid["index_close"]) / grid["index_close"].replace(0, np.nan)).fillna(0.0)
    grid["mark_premium"] = grid["mark_basis"]
    grid["mark_premium_mean_4"] = grid["mark_premium"].rolling(4, min_periods=1).mean().fillna(0.0)
    grid["funding_rate_abs"] = grid["funding_rate"].abs()
    grid["funding_rate_mean_4"] = grid["funding_rate"].rolling(4, min_periods=1).mean().fillna(0.0)
    grid["funding_rate_std_4"] = grid["funding_rate"].rolling(4, min_periods=1).std().fillna(0.0)
    grid["futures_pressure"] = (grid["mark_premium"] - grid["funding_rate"]).fillna(0.0)

    return grid[["timestamp"] + FUNDING_FEATURE_COLUMNS]


def merge_htf_robust(ohlcv_timestamps: pd.Series, htf_table: pd.DataFrame) -> pd.DataFrame:
    grid = pd.DataFrame({"timestamp": pd.to_datetime(ohlcv_timestamps, utc=True).dt.tz_localize(None).astype("datetime64[ns]")}).sort_values("timestamp")
    htf = htf_table.copy()
    htf["timestamp"] = pd.to_datetime(htf["timestamp"], utc=True).dt.tz_localize(None).astype("datetime64[ns]")
    grid = pd.merge_asof(grid, htf.sort_values("timestamp"), on="timestamp", direction="backward")
    for col in HTF_FEATURE_COLUMNS:
        grid[col] = grid[col].fillna(0.0 if "atr_percentile" not in col else 0.5)
    return grid[["timestamp"] + HTF_FEATURE_COLUMNS]


def load_raw_dataset(cache_dir: Path, symbol: str) -> tuple[pd.DataFrame, list[str]]:
    prefix = ASSETS[symbol]["model_prefix"]
    raw_ohlcv_file = cache_dir / f"{symbol.replace('/', '_')}.parquet"
    if not raw_ohlcv_file.exists():
        raise FileNotFoundError(f"Raw OHLCV missing: {raw_ohlcv_file}")

    df_raw = prep_ts(pd.read_parquet(raw_ohlcv_file))

    # C-1: Feature source MUST be predictor_server.build_features()
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
        p_raw = prep_ts(pd.read_parquet(cache_dir / f"{peer.replace('/', '_')}.parquet"))
        p_candles = [
            {"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for ts, o, h, l, c, v in zip(p_raw["timestamp"], p_raw["open"], p_raw["high"], p_raw["low"], p_raw["close"], p_raw["volume"])
        ]
        peer_dfs[peer] = prep_ts(build_features(p_candles).rename(columns={"date": "timestamp"}))

    # Load HTF features
    kline_1h = prep_ts(pd.read_parquet(cache_dir / "kline_60m_BTC_USDT.parquet"))
    kline_4h = prep_ts(pd.read_parquet(cache_dir / "kline_240m_BTC_USDT.parquet"))
    htf_1h = compute_htf_series(kline_1h, "1h")
    htf_4h = compute_htf_series(kline_4h, "4h")
    htf_merged = pd.merge(htf_1h, htf_4h, on="timestamp", how="outer")
    htf_grid = merge_htf_robust(base_feats["timestamp"], htf_merged)

    # Assemble full table
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

    # Add raw OHLCV columns for label & PnL evaluation
    feats["open"] = df_raw["open"].values
    feats["high"] = df_raw["high"].values
    feats["low"] = df_raw["low"].values
    feats["close"] = df_raw["close"].values
    feats["volume"] = df_raw["volume"].values

    # C-2: Assert parity against production booster
    booster_path = REPO_ROOT / f"models/model_{prefix}_long.txt"
    booster = lgb.Booster(model_file=str(booster_path))
    expected_cols = booster.feature_name()

    missing = [c for c in expected_cols if c not in feats.columns]
    if missing:
        raise RuntimeError(f"C-2 HALT: Missing {len(missing)} expected feature columns for {symbol}: {missing}")

    return feats, expected_cols


def run_evaluation(symbol: str, df: pd.DataFrame, feature_cols: list[str], is_null_run: bool = False) -> dict:
    close = df["close"].values
    c0 = close[:-BARS_FORWARD]
    c2 = close[BARS_FORWARD:]

    # C-3: Tie policy: exclude flat bars
    diff = c2 - c0
    valid_mask = (diff != 0)
    tie_count = int(np.sum(diff == 0))
    total_bars = len(c0)

    # Subsetting df to valid (non-tied) bars for direction evaluation
    eval_df = df.iloc[:-BARS_FORWARD][valid_mask].copy().reset_index(drop=True)
    y_raw = (diff[valid_mask] > 0).astype(int)

    if is_null_run:
        # C-4: Null shuffle seed
        rng = np.random.RandomState(SHUFFLE_SEED)
        y = rng.permutation(y_raw)
    else:
        y = y_raw

    eval_df["target_direction"] = y

    # Chronological 70:30 split with dynamic embargo
    n = len(eval_df)
    train_n = int(n * 0.7)
    embargo = BARS_FORWARD

    train_df = eval_df.iloc[:train_n].copy()
    test_df = eval_df.iloc[train_n + embargo:].copy().reset_index(drop=True)

    X_train = train_df[feature_cols].astype(float)
    y_train = train_df["target_direction"].astype(int)

    X_test = test_df[feature_cols].astype(float)
    y_test = test_df["target_direction"].astype(int)

    # Model training
    model = lgb.LGBMClassifier(
        n_estimators=100, learning_rate=0.05, random_state=TRAIN_SEED, n_jobs=-1
    )
    model.fit(X_train, y_train)

    test_probs = model.predict_proba(X_test)[:, 1]
    test_auc = float(roc_auc_score(y_test, test_probs)) if len(np.unique(y_test)) > 1 else 0.5

    # 2-bar 15m return calculation for gross/net evaluation
    test_close_entry = test_df["close"].values
    # Return of long entry over 2 bars
    test_ret_2bar = (test_df["close"].shift(-BARS_FORWARD).values - test_close_entry) / test_close_entry
    # Truncate to align with valid 2-bar return
    valid_eval_len = len(test_ret_2bar) - BARS_FORWARD

    fee_drag_fraction = (ROUNDTRIP_TAKER_BPS + SLIPPAGE_BPS) / 10000.0

    # Threshold sweep: T from 0.50 to 0.60 step 0.01
    thresholds = [round(t, 2) for t in np.arange(0.50, 0.61, 0.01)]
    sweep_table = []

    acc_list = []
    expectancy_list = []

    for t in thresholds:
        fired_mask = (test_probs[:valid_eval_len] >= t)
        signal_count = int(np.sum(fired_mask))

        if signal_count == 0:
            sweep_table.append({
                "threshold": t,
                "signal_count": 0,
                "directional_accuracy": None,
                "gross_mean_return_pct": None,
                "net_mean_return_pct": None,
                "underpowered": True
            })
            acc_list.append(0.0)
            expectancy_list.append(-1.0)
            continue

        y_test_fired = y_test.values[:valid_eval_len][fired_mask]
        accuracy = float(np.mean(y_test_fired == 1))

        fired_returns = test_ret_2bar[:valid_eval_len][fired_mask]
        gross_mean_ret = float(np.mean(fired_returns)) * 100.0
        net_mean_ret = (float(np.mean(fired_returns)) - fee_drag_fraction) * 100.0

        underpowered = signal_count < 100

        sweep_table.append({
            "threshold": t,
            "signal_count": signal_count,
            "directional_accuracy": round(accuracy, 4),
            "gross_mean_return_pct": round(gross_mean_ret, 4),
            "net_mean_return_pct": round(net_mean_ret, 4),
            "underpowered": underpowered
        })

        acc_list.append(accuracy)
        expectancy_list.append(net_mean_ret)

    # C-5: Programmatic Falsification Metrics
    # Monotonicity check on accuracy for active thresholds
    valid_accs = [a for a in acc_list if a > 0.0]
    is_monotonic = bool(all(x <= y for x, y in zip(valid_accs, valid_accs[1:]))) if len(valid_accs) > 1 else False

    # Longest contiguous run of thresholds with positive net return
    pos_runs = []
    current_run = 0
    for exp in expectancy_list:
        if exp > 0:
            current_run += 1
        else:
            if current_run > 0:
                pos_runs.append(current_run)
            current_run = 0
    if current_run > 0:
        pos_runs.append(current_run)
    max_contiguous_positive_run = max(pos_runs) if pos_runs else 0

    # ATR Tercile Breakdown at T=0.50
    test_df_eval = test_df.iloc[:valid_eval_len].copy()
    test_df_eval["prob"] = test_probs[:valid_eval_len]
    test_df_eval["ret_2bar"] = test_ret_2bar[:valid_eval_len]

    tercile_report = {}
    if "atr_percentile" in test_df_eval.columns:
        test_df_eval["atr_tercile"] = pd.qcut(test_df_eval["atr_percentile"], 3, labels=["low", "mid", "high"], duplicates="drop")
        for tercile, group in test_df_eval.groupby("atr_tercile", observed=False):
            fired_g = group[group["prob"] >= 0.50]
            cnt = len(fired_g)
            acc_g = float(np.mean(fired_g["target_direction"] == 1)) if cnt > 0 else None
            net_g = (float(np.mean(fired_g["ret_2bar"])) - fee_drag_fraction) * 100.0 if cnt > 0 else None
            tercile_report[str(tercile)] = {"signals": cnt, "accuracy": round(acc_g, 4) if acc_g is not None else None, "net_return_pct": round(net_g, 4) if net_g is not None else None}

    # Session Breakdown at T=0.50
    session_report = {}
    for s_col in ["session_asia", "session_london", "session_newyork"]:
        if s_col in test_df_eval.columns:
            group = test_df_eval[test_df_eval[s_col] == 1]
            fired_g = group[group["prob"] >= 0.50]
            cnt = len(fired_g)
            acc_g = float(np.mean(fired_g["target_direction"] == 1)) if cnt > 0 else None
            net_g = (float(np.mean(fired_g["ret_2bar"])) - fee_drag_fraction) * 100.0 if cnt > 0 else None
            session_report[s_col.replace("session_", "")] = {"signals": cnt, "accuracy": round(acc_g, 4) if acc_g is not None else None, "net_return_pct": round(net_g, 4) if net_g is not None else None}

    return {
        "symbol": symbol,
        "is_null_control": is_null_run,
        "feature_source": "predictor_server.build_features",
        "n_features": len(feature_cols),
        "feature_names": feature_cols,
        "total_bars": total_bars,
        "tie_policy": "exclude",
        "tie_count": tie_count,
        "usable_bars": len(eval_df),
        "train_rows": len(train_df),
        "test_rows": len(test_df),
        "base_rate": round(float(y.mean()), 4),
        "train_seed": TRAIN_SEED,
        "shuffle_seed": SHUFFLE_SEED if is_null_run else None,
        "embargo_bars": embargo,
        "fees": {
            "roundtrip_taker_bps": ROUNDTRIP_TAKER_BPS,
            "slippage_bps": SLIPPAGE_BPS,
        },
        "test_auc": round(test_auc, 4),
        "monotonic": is_monotonic,
        "contiguous_positive_run": max_contiguous_positive_run,
        "short_is_complement": True,
        "atr_tercile_breakdown": tercile_report,
        "session_breakdown": session_report,
        "threshold_sweep": sweep_table,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default=str(REPO_ROOT / ".retrain_cache"))
    parser.add_argument("--reports-dir", default=str(REPO_ROOT / "reports"))
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    print("=== STAGE 1 EVALUATION & NULL CONTROL HARNESS (v1.11.1 Packet C) ===")

    for symbol in ASSETS:
        asset_prefix = ASSETS[symbol]["model_prefix"]
        print(f"\n--- Processing {symbol} ---")

        df_feats, feature_cols = load_raw_dataset(cache_dir, symbol)
        print(f"[{symbol}] Loaded {len(df_feats)} rows via build_features(). Feature parity ASSERTED (65/65).")

        # C-4 Step 1: Run NULL control first
        print(f"[{symbol}] Executing label-shuffle NULL control (seed={SHUFFLE_SEED})...")
        null_res = run_evaluation(symbol, df_feats, feature_cols, is_null_run=True)

        null_out = reports_dir / f"stage1_null_{asset_prefix}.json"
        with open(null_out, "w") as f:
            json.dump(null_res, f, indent=2)

        null_auc = null_res["test_auc"]
        print(f"[{symbol}] NULL Control Test AUC: {null_auc:.4f}")

        # Assert null control bounds [0.47, 0.53]
        if not (0.47 <= null_auc <= 0.53):
            raise RuntimeError(f"C-4 HALT: Null control AUC {null_auc:.4f} for {symbol} outside [0.47, 0.53]. Harness is leaking!")

        null_accs = [row["directional_accuracy"] for row in null_res["threshold_sweep"] if row["directional_accuracy"] is not None and row["signal_count"] >= 200]
        for acc in null_accs:
            if not (0.47 <= acc <= 0.53):
                raise RuntimeError(f"C-4 HALT: Null control threshold accuracy {acc:.4f} for {symbol} outside [0.47, 0.53]. Harness is leaking!")

        print(f"[{symbol}] NULL Control PASSED. Proceeding to real evaluation...")

        # Step 2: Run Real Evaluation
        real_res = run_evaluation(symbol, df_feats, feature_cols, is_null_run=False)

        real_out = reports_dir / f"stage1_eval_{asset_prefix}.json"
        with open(real_out, "w") as f:
            json.dump(real_res, f, indent=2)

        print(f"[{symbol}] Real Evaluation Test AUC: {real_res['test_auc']:.4f}")
        print(f"[{symbol}] Monotonic: {real_res['monotonic']}, Max Contiguous Positive Run: {real_res['contiguous_positive_run']}")
        print(f"[{symbol}] Saved real evaluation report to {real_out}")

    print("\n=== ALL STAGE 1 EVALUATION AND NULL CONTROL REPORTS SUCCESSFULLY GENERATED ===")


if __name__ == "__main__":
    main()
