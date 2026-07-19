#!/usr/bin/env python3
"""
tools/retrain_live_features.py — H-13 remediation, retrain path.

Retrains all six models (BTC/ETH/SOL x long/short) using ONLY the feature
columns predictor_server.py's own build_features() can actually compute
live from a single 15m OHLCV buffer — the same 41 columns the H-13 P0
audit classified as LIVE (primary_basic + primary_structure_smc).

This deliberately imports build_features() directly from
src/predictor_server.py rather than reusing lgbm_poc/features.py's
build_feature_table(), which is a *different* implementation that also
computes futures/funding columns predictor_server.py never populates live.
Using the live module as the single source of truth for training features
is the actual fix for the root cause of H-13: the training-side and
serving-side feature builders had drifted apart. This removes that
possibility by construction — the trained feature_names always come from
the exact function that will run at inference time.

Threshold selection: F1-maximizing probability threshold on the
time-ordered validation split, per asset/direction. Exit threshold is set
to 0.8x the entry threshold (matches the ~80% ratio in the previous
production thresholds). This is a fast, defensible starting point, not a
substitute for the proper PAPER_BASELINE walk-forward calibration process
— re-calibrate against paper_baseline results once that window runs.

Usage:
    python3 tools/retrain_live_features.py --days 90
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import _bootstrap  # noqa: F401  (adds src/ to path for lgbm_poc imports)
from lgbm_poc.labels import label_tp_before_sl_1h, label_short_tp_before_sl_1h
from lgbm_poc.train import TrainConfig, train_binary_model
from download_ohlcv import fetch_ohlcv

# Import the live feature builder directly — the actual fix.
from predictor_server import build_features  # noqa: E402

# The 41 columns the H-13 P0 audit classified LIVE (primary_basic 24 +
# primary_structure_smc 17). Anything build_features() computes beyond
# this (alias columns, intermediates like ema_fast/vol_mean_20) is
# deliberately excluded — those aren't in the original trained schema and
# including them would just be a different kind of drift.
LIVE_FEATURE_COLUMNS = [
    # primary_basic (24)
    "log_return_1", "log_return_3", "log_return_6", "range_1", "atr_proxy",
    "volatility_lookback", "hour_of_day", "day_of_week", "session_asia",
    "session_london", "session_newyork", "volume_zscore", "relative_volume",
    "volume_percentile", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "atr_normalized_range", "stop_distance", "dist_ema_fast", "dist_ema_slow",
    "trend_strength", "trend_direction", "ema_slow_slope",
    # primary_structure_smc (17)
    "sweep_high_detected", "sweep_low_detected", "sweep_depth_atr",
    "sweep_rejection_ratio", "sweep_volume_zscore", "bullish_fvg_present",
    "bearish_fvg_present", "fvg_size_atr", "fvg_age_candles",
    "price_inside_fvg", "breakout_volume_confirmation",
    "rejection_volume_confirmation", "volume_block_strength",
    "atr_percentile", "range_compression", "high_volatility_flag",
    "market_regime",
]
assert len(LIVE_FEATURE_COLUMNS) == 41

ASSETS = {
    "BTC/USDT": {"model_prefix": "btc", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
    "ETH/USDT": {"model_prefix": "eth", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
    "SOL/USDT": {"model_prefix": "sol", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
}
HORIZON_BARS = 4  # matches config.json max_candles_held


def best_f1_threshold(y_true: np.ndarray, y_prob: np.ndarray) -> tuple[float, dict]:
    """Scan candidate thresholds, return the one maximizing F1 on this split."""
    candidates = np.unique(np.quantile(y_prob, np.linspace(0.5, 0.98, 49)))
    best = (0.5, -1.0, {})
    for t in candidates:
        pred = (y_prob >= t).astype(int)
        tp = int(((pred == 1) & (y_true == 1)).sum())
        fp = int(((pred == 1) & (y_true == 0)).sum())
        fn = int(((pred == 0) & (y_true == 1)).sum())
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        fire_rate = float(pred.mean())
        if f1 > best[1] and fire_rate > 0.01:  # must fire on >1% of candles or it's useless
            best = (float(t), f1, {"precision": precision, "recall": recall,
                                    "f1": f1, "fire_rate": fire_rate})
    return best[0], best[2]


def time_split(df: pd.DataFrame, train=0.7, valid=0.15):
    n = len(df)
    a, b = int(n * train), int(n * (train + valid))
    return df.iloc[:a], df.iloc[a:b], df.iloc[b:]


def cached_fetch_ohlcv(exchange: str, symbol: str, since_ms: int, cache_dir: Path,
                        budget_s: float = 35.0) -> pd.DataFrame:
    """Resumable OHLCV fetch: caches to parquet, continues from the last
    cached candle on repeated calls instead of re-fetching from scratch.
    Stops after budget_s seconds (leaving time for the rest of the script
    to run within one sandbox call) — call the script again to continue;
    it'll pick up right where it left off."""
    import time as _time
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol.replace('/', '_')}.parquet"

    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        cursor = int(cached["timestamp"].max().timestamp() * 1000) + 1
        print(f"  resuming from cache: {len(cached)} candles, last={cached['timestamp'].max()}")
        # Already caught up — skip the fetch loop entirely so the time
        # budget goes to whichever asset actually still needs it.
        if cached["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=30):
            print(f"  already caught up, skipping fetch")
            return cached
    else:
        cached = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        cursor = since_ms

    t0 = _time.monotonic()
    new_rows = []
    while _time.monotonic() - t0 < budget_s:
        batch = fetch_ohlcv(exchange, symbol, "15m", cursor, limit=1000,
                             market_type="linear", max_rows=1000)
        if batch.empty:
            break
        new_rows.append(batch)
        cursor = int(batch["timestamp"].max().timestamp() * 1000) + 1
        if batch["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=20):
            break  # caught up to near-live

    if new_rows:
        combined = pd.concat([cached, *new_rows], ignore_index=True)
        combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        combined.to_parquet(cache_path)
        cached = combined

    caught_up = cached["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=30) if len(cached) else False
    print(f"  cache now has {len(cached)} candles, up to {cached['timestamp'].max() if len(cached) else 'n/a'} "
          f"({'CAUGHT UP' if caught_up else 'MORE NEEDED — rerun'})")
    return cached


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--exchange", default="bybit")
    parser.add_argument("--models-dir", default=str(REPO_ROOT / "models"))
    parser.add_argument("--cache-dir", default="/tmp/ohlcv_cache")
    parser.add_argument("--fetch-only", action="store_true",
                         help="Only fetch/cache OHLCV, skip training (for resumable multi-call fetching).")
    args = parser.parse_args()

    since_ms = int((pd.Timestamp.utcnow() - pd.Timedelta(days=args.days)).timestamp() * 1000)
    models_dir = Path(args.models_dir)
    cache_dir = Path(args.cache_dir)
    results = {}
    all_caught_up = True

    for symbol, cfg in ASSETS.items():
        print(f"\n=== {symbol} ===")
        raw = cached_fetch_ohlcv(args.exchange, symbol, since_ms, cache_dir,
                                  budget_s=38.0)  # per-asset cap; already-caught-up assets return instantly
        caught_up = raw["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=30) if len(raw) else False
        all_caught_up = all_caught_up and caught_up
        print(f"  using {len(raw)} candles ({raw['timestamp'].min()} .. {raw['timestamp'].max()})")

        if args.fetch_only:
            continue

        candles = [
            {"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for ts, o, h, l, c, v in zip(raw["timestamp"], raw["open"], raw["high"],
                                          raw["low"], raw["close"], raw["volume"])
        ]
        feats = build_features(candles)
        feats = feats.rename(columns={"date": "timestamp"})
        missing = [c for c in LIVE_FEATURE_COLUMNS if c not in feats.columns]
        if missing:
            raise RuntimeError(f"build_features() did not produce expected columns: {missing}")

        labeled = label_tp_before_sl_1h(feats, horizon_bars=HORIZON_BARS,
                                         tp_atr_mult=cfg["tp_atr_mult"], sl_atr_mult=cfg["sl_atr_mult"])
        labeled = label_short_tp_before_sl_1h(labeled, horizon_bars=HORIZON_BARS,
                                               tp_atr_mult=cfg["tp_atr_mult"], sl_atr_mult=cfg["sl_atr_mult"])
        labeled = labeled.dropna(subset=["label_tp_before_sl_1h", "label_short_tp_before_sl_1h"]).reset_index(drop=True)
        print(f"  {len(labeled)} labeled rows; long positive rate={labeled['label_tp_before_sl_1h'].mean():.3f}, "
              f"short positive rate={labeled['label_short_tp_before_sl_1h'].mean():.3f}")

        for direction, label_col in [("long", "label_tp_before_sl_1h"), ("short", "label_short_tp_before_sl_1h")]:
            train_df, valid_df, test_df = time_split(labeled)

            def xy(d):
                X = d[LIVE_FEATURE_COLUMNS].astype(float)
                y = d[label_col].astype(int)
                return X, y

            X_train, y_train = xy(train_df)
            X_valid, y_valid = xy(valid_df)
            X_test, y_test = xy(test_df)

            import lightgbm as lgb
            model = lgb.LGBMClassifier(
                objective="binary", num_leaves=31, learning_rate=0.05, n_estimators=500,
                min_child_samples=50, subsample=0.9, colsample_bytree=0.9, random_state=42, n_jobs=-1,
            )
            model.fit(X_train, y_train, eval_set=[(X_valid, y_valid)], eval_metric="binary_logloss",
                      callbacks=[lgb.early_stopping(30, verbose=False)])

            valid_prob = model.predict_proba(X_valid)[:, 1]
            entry_thr, valid_metrics = best_f1_threshold(y_valid.to_numpy(), valid_prob)
            exit_thr = round(entry_thr * 0.8, 4)

            test_prob = model.predict_proba(X_test)[:, 1]
            test_pred = (test_prob >= entry_thr).astype(int)
            from sklearn.metrics import roc_auc_score
            test_auc = roc_auc_score(y_test, test_prob) if y_test.nunique() > 1 else None
            test_precision = float(((test_pred == 1) & (y_test == 1)).sum() / max(1, test_pred.sum()))
            test_fire_rate = float(test_pred.mean())

            model_name = f"model_{cfg['model_prefix']}_{direction}"
            out_path = models_dir / f"{model_name}.txt"
            model.booster_.save_model(str(out_path))
            print(f"  [{direction}] entry_thr={entry_thr:.4f} exit_thr={exit_thr:.4f} "
                  f"valid_f1={valid_metrics.get('f1', 0):.3f} test_auc={test_auc} "
                  f"test_precision={test_precision:.3f} test_fire_rate={test_fire_rate:.3f} "
                  f"-> {out_path}")

            results[model_name] = {
                "symbol": symbol, "direction": direction,
                "feature_count": len(LIVE_FEATURE_COLUMNS),
                "entry_threshold": entry_thr, "exit_threshold": exit_thr,
                "valid_metrics": valid_metrics,
                "test_auc": test_auc, "test_precision": test_precision,
                "test_fire_rate": test_fire_rate,
                "train_rows": len(train_df), "valid_rows": len(valid_df), "test_rows": len(test_df),
            }

    if args.fetch_only:
        print(f"\n{'ALL CAUGHT UP' if all_caught_up else 'NOT CAUGHT UP YET — rerun with --fetch-only to continue'}")
        return 0

    out_meta = models_dir / "retrain_live_features_report.json"
    out_meta.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"\nWrote {out_meta}")
    print(f"{'(all data was CAUGHT UP as of Jan-1 start)' if all_caught_up else '(WARNING: not all assets were fully caught up — see per-asset cache status above)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
