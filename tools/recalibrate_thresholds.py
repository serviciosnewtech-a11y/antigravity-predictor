#!/usr/bin/env python3
"""
tools/recalibrate_thresholds.py — recompute entry/exit thresholds for the
already-trained H-13 retrain models, without retraining.

Why this exists: the original thresholds (tools/retrain_live_features.py)
were F1-maximized against the TP/SL-hit label, which is a rare event
(~7-12% positive rate). Precision/recall optimization against a rare
label naturally lands on a threshold near the tail of the model's own
probability distribution — technically sound for classification metrics,
but it means the live probability rarely crosses it at any given moment,
so the dashboard mostly shows NEUTRAL even though the model is working.

This script targets firing frequency directly: threshold = the
percentile of the model's OWN predicted-probability distribution on the
validation split that corresponds to the desired fire rate. That's a
direct, legible knob (--fire-rate) instead of an indirect side effect of
label rarity.

Uses the existing cached OHLCV (.retrain_cache/) and existing model
files (models/model_*.txt) — no network fetch, no retraining.

Usage:
    python3 tools/recalibrate_thresholds.py --fire-rate 0.22
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import _bootstrap  # noqa: F401
from predictor_server import build_features  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrain_live_features import (  # noqa: E402
    ASSETS, BASE_LIVE_FEATURE_COLUMNS, time_split,
    fetch_all_expanded, assemble_expanded_table, feature_columns_for,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fire-rate", type=float, default=0.22,
                         help="Target fraction of candles the entry signal should fire on (0-1).")
    parser.add_argument("--exit-ratio", type=float, default=0.8,
                         help="Exit threshold as a fraction of entry threshold.")
    parser.add_argument("--cache-dir", default=str(REPO_ROOT / ".retrain_cache"))
    parser.add_argument("--config", default=str(REPO_ROOT / "config.json"))
    parser.add_argument("--models-dir", default=str(REPO_ROOT / "models"))
    parser.add_argument("--no-expand", action="store_true",
                         help="Recalibrate against the base-41-only feature set (matches models trained with --no-expand).")
    parser.add_argument("--days", type=int, default=90,
                         help="Only used to bound the funding/mark/index re-fetch if the cache needs refreshing.")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    models_dir = Path(args.models_dir)
    config_path = Path(args.config)
    config = json.loads(config_path.read_text())

    results = {}

    if args.no_expand:
        base_feats, funding_tables, htf_table = {}, {}, None
        for symbol in ASSETS:
            cache_path = cache_dir / f"{symbol.replace('/', '_')}.parquet"
            raw = pd.read_parquet(cache_path)
            candles = [
                {"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v}
                for ts, o, h, l, c, v in zip(raw["timestamp"], raw["open"], raw["high"],
                                              raw["low"], raw["close"], raw["volume"])
            ]
            base_feats[symbol] = build_features(candles).rename(columns={"date": "timestamp"})
    else:
        # Reuses whatever is already cached in cache_dir (OHLCV, funding
        # history, mark/index klines, BTC 1h/4h klines) — network calls
        # only happen for whatever's missing or stale, same resumable
        # pattern as the retrain script itself.
        _, base_feats, funding_tables, htf_table, _ = fetch_all_expanded(args.days, "bybit", cache_dir)

    for symbol, cfg in ASSETS.items():
        if args.no_expand:
            feats = base_feats[symbol]
            feature_columns = BASE_LIVE_FEATURE_COLUMNS
        else:
            feats, feature_columns = assemble_expanded_table(symbol, base_feats, funding_tables, htf_table)

        # Same time_split as training so "validation" here is the same
        # slice the model's threshold was originally chosen against.
        train_df, valid_df, test_df = time_split(feats.dropna().reset_index(drop=True))
        X_valid = valid_df[feature_columns].astype(float)

        for direction in ("long", "short"):
            model_name = f"model_{cfg['model_prefix']}_{direction}"
            model_path = models_dir / f"{model_name}.txt"
            booster = lgb.Booster(model_file=str(model_path))
            prob = booster.predict(X_valid)

            entry_thr = float(np.quantile(prob, 1.0 - args.fire_rate))
            exit_thr = round(entry_thr * args.exit_ratio, 4)
            actual_fire_rate = float((prob >= entry_thr).mean())

            results[model_name] = {
                "symbol": symbol, "direction": direction,
                "entry_threshold": round(entry_thr, 4), "exit_threshold": exit_thr,
                "target_fire_rate": args.fire_rate, "actual_fire_rate": round(actual_fire_rate, 4),
                "prob_min": round(float(prob.min()), 4), "prob_max": round(float(prob.max()), 4),
                "prob_median": round(float(np.median(prob)), 4),
            }
            print(f"{model_name}: entry={entry_thr:.4f} exit={exit_thr:.4f} "
                  f"fire_rate={actual_fire_rate:.3f} (prob range {prob.min():.4f}-{prob.max():.4f}, "
                  f"median {np.median(prob):.4f})")

    # Update config.json in place.
    asset_key_map = {"BTC/USDT": "BTC/USDT", "ETH/USDT": "ETH/USDT", "SOL/USDT": "SOL/USDT"}
    for symbol in ASSETS:
        prefix = ASSETS[symbol]["model_prefix"]
        long_r = results[f"model_{prefix}_long"]
        short_r = results[f"model_{prefix}_short"]
        a = config["assets"][symbol]
        a["buy_threshold"] = long_r["entry_threshold"]
        a["exit_threshold"] = long_r["exit_threshold"]
        a["sell_threshold"] = short_r["entry_threshold"]
        a["exit_short_threshold"] = short_r["exit_threshold"]
        a["_note"] = (
            f"H-13 retrain 2026-07-19, thresholds recalibrated to a target "
            f"{args.fire_rate:.0%} fire rate (percentile of the model's own "
            f"validation-set probability distribution, not F1-maximized against "
            f"the rare TP/SL label — that approach produced thresholds the live "
            f"probability rarely crossed, i.e. permanent NEUTRAL). Actual fire "
            f"rates: long={long_r['actual_fire_rate']:.0%}, short={short_r['actual_fire_rate']:.0%}. "
            f"Still a fast calibration pass, not a PAPER_BASELINE result — a higher "
            f"fire rate trades away selectivity for visibility; re-tune before "
            f"anything trades real money."
        )

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    out_report = models_dir / "recalibrate_thresholds_report.json"
    out_report.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"\nWrote {config_path} and {out_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
