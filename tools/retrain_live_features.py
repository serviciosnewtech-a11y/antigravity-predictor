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
import time as _time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import _bootstrap  # noqa: F401  (adds src/ to path for lgbm_poc imports)
from lgbm_poc.labels import label_tp_before_sl_1h, label_short_tp_before_sl_1h
from lgbm_poc.train import TrainConfig, train_binary_model
from download_ohlcv import fetch_ohlcv

# Import the live feature builder directly — the actual fix.
from predictor_server import build_features, FUNDING_FEATURE_COLUMNS, PEER_FEATURE_SUFFIXES  # noqa: E402

# The 41 columns the H-13 P0 audit classified LIVE (primary_basic 24 +
# primary_structure_smc 17). Anything build_features() computes beyond
# this (alias columns, intermediates like ema_fast/vol_mean_20) is
# deliberately excluded — those aren't in the original trained schema and
# including them would just be a different kind of drift.
BASE_LIVE_FEATURE_COLUMNS = [
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
assert len(BASE_LIVE_FEATURE_COLUMNS) == 41
# LIVE_FEATURE_COLUMNS kept as an alias for anything (e.g.
# tools/recalibrate_thresholds.py) that imported the old name expecting the
# base 41. New code should use per-asset feature_columns_for(symbol) below.
LIVE_FEATURE_COLUMNS = BASE_LIVE_FEATURE_COLUMNS

ASSETS = {
    "BTC/USDT": {"model_prefix": "btc", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
    "ETH/USDT": {"model_prefix": "eth", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
    "SOL/USDT": {"model_prefix": "sol", "tp_atr_mult": 1.5, "sl_atr_mult": 1.0},
}
HORIZON_BARS = 4  # matches config.json max_candles_held


def peer_prefixes_for(symbol: str) -> list[str]:
    """Same rule as AssetEngine.peer_prefixes in predictor_server.py: the
    other two assets' tickers, lowercased, in ASSETS iteration order."""
    return [s.split("/")[0].lower() for s in ASSETS if s != symbol]


def feature_columns_for(symbol: str) -> list[str]:
    """The full 57-column live feature list for one asset's model: the
    base 41 (shared formula, shared code — true single source of truth)
    plus the 8 funding columns plus 8 cross-asset columns named for
    whichever two peers aren't `symbol`."""
    peer_cols = [f"{p}_{suf}" for p in peer_prefixes_for(symbol) for suf in PEER_FEATURE_SUFFIXES]
    return BASE_LIVE_FEATURE_COLUMNS + FUNDING_FEATURE_COLUMNS + peer_cols


# ── Historical funding-rate + mark/index price fetch (Bybit public REST) ────
# NOTE on parity with the live path: predictor_server.py's
# AssetEngine._build_funding_ctx() computes these 8 columns from a live
# snapshot + a small in-memory rolling window (see its docstring). Here we
# recompute the SAME formulas (mark_basis=(mark-index)/index,
# funding_rate_mean_4/std_4 as rolling means/stds, futures_pressure =
# mark_premium - funding_rate) but as full historical time series instead
# of a live single-snapshot broadcast. This is the one place in the H-13
# feature-expansion work that is NOT literally the same code running both
# places — it's the same formula, independently implemented for the
# historical-series case. Flagging this explicitly rather than claiming
# full code-level single-source-of-truth for these two families the way
# the base 41 columns have it.

_BYBIT_BASE = "https://api.bybit.com/v5/market"


def cached_fetch_funding_history(symbol: str, since_ms: int, cache_dir: Path,
                                  budget_s: float = 15.0) -> pd.DataFrame:
    """Bybit funding-rate-history, paginated backward from now via endTime,
    cached to parquet, resumable across calls like cached_fetch_ohlcv."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"funding_{symbol.replace('/', '_')}.parquet"
    bybit_sym = symbol.replace("/", "")

    cached = pd.read_parquet(cache_path) if cache_path.exists() else pd.DataFrame(columns=["timestamp", "funding_rate"])
    have_since = int(cached["timestamp"].min().timestamp() * 1000) if len(cached) else int(pd.Timestamp.utcnow().timestamp() * 1000)

    if len(cached) and have_since <= since_ms:
        return cached[cached["timestamp"] >= pd.Timestamp(since_ms, unit="ms", tz="UTC")]

    t0 = _time.monotonic()
    end_cursor = have_since
    rows = []
    while _time.monotonic() - t0 < budget_s and end_cursor > since_ms:
        r = requests.get(f"{_BYBIT_BASE}/funding/history",
                          params={"category": "linear", "symbol": bybit_sym, "limit": 200, "endTime": end_cursor},
                          timeout=15)
        data = r.json().get("result", {}).get("list", [])
        if not data:
            break
        for row in data:
            rows.append({"timestamp": pd.Timestamp(int(row["fundingRateTimestamp"]), unit="ms", tz="UTC"),
                         "funding_rate": float(row["fundingRate"])})
        oldest = min(int(row["fundingRateTimestamp"]) for row in data)
        if oldest >= end_cursor:
            break
        end_cursor = oldest

    if rows:
        new_df = pd.DataFrame(rows)
        combined = pd.concat([cached, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        combined.to_parquet(cache_path)
        cached = combined
    return cached


def cached_fetch_price_kline(symbol: str, kind: str, since_ms: int, cache_dir: Path,
                              interval_min: int = 15, budget_s: float = 30.0) -> pd.DataFrame:
    """Bybit mark-price-kline or index-price-kline (kind='mark'|'index'),
    paginated forward via startTime like cached_fetch_ohlcv, resumable."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{kind}_{symbol.replace('/', '_')}.parquet"
    bybit_sym = symbol.replace("/", "")
    endpoint = "mark-price-kline" if kind == "mark" else "index-price-kline"

    cached = pd.read_parquet(cache_path) if cache_path.exists() else pd.DataFrame(columns=["timestamp", "close"])
    cursor = int(cached["timestamp"].max().timestamp() * 1000) + 1 if len(cached) else since_ms

    if len(cached) and cached["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=30):
        return cached

    t0 = _time.monotonic()
    new_rows = []
    while _time.monotonic() - t0 < budget_s:
        r = requests.get(f"{_BYBIT_BASE}/{endpoint}",
                          params={"category": "linear", "symbol": bybit_sym, "interval": str(interval_min),
                                  "start": cursor, "limit": 1000},
                          timeout=15)
        data = r.json().get("result", {}).get("list", [])
        if not data:
            break
        batch = pd.DataFrame([
            {"timestamp": pd.Timestamp(int(row[0]), unit="ms", tz="UTC"), "close": float(row[4])}
            for row in data
        ]).sort_values("timestamp")
        new_rows.append(batch)
        newest = int(batch["timestamp"].max().timestamp() * 1000)
        if newest <= cursor:
            break
        cursor = newest + 1
        if batch["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=20):
            break

    if new_rows:
        combined = pd.concat([cached, *new_rows], ignore_index=True)
        combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        combined.to_parquet(cache_path)
        cached = combined
    return cached


def compute_funding_feature_table(ohlcv_timestamps: pd.Series, funding_hist: pd.DataFrame,
                                   mark_kline: pd.DataFrame, index_kline: pd.DataFrame) -> pd.DataFrame:
    """Align funding-rate-history (8h grid) and mark/index klines (15m grid)
    onto the same 15m timestamps as the OHLCV/base-feature table, then
    compute the 8 FUNDING_FEATURE_COLUMNS exactly per the formulas in
    AssetEngine._build_funding_ctx()."""
    # ohlcv_timestamps comes from build_features()'s "date" column, which is
    # tz-naive (pd.to_datetime(unix_seconds, unit="s")). The Bybit fetch
    # helpers above build tz-aware UTC timestamps. merge_asof requires both
    # sides to match, so normalize everything to tz-naive UTC here.
    grid = pd.DataFrame({"timestamp": pd.to_datetime(ohlcv_timestamps).dt.tz_localize(None)}).sort_values("timestamp")

    funding = funding_hist.sort_values("timestamp").copy()
    funding["timestamp"] = funding["timestamp"].dt.tz_localize(None)
    grid = pd.merge_asof(grid, funding.rename(columns={"funding_rate": "funding_rate"}),
                          on="timestamp", direction="backward")
    grid["funding_rate"] = grid["funding_rate"].fillna(0.0)

    mark = mark_kline.sort_values("timestamp").rename(columns={"close": "mark_close"}).copy()
    mark["timestamp"] = mark["timestamp"].dt.tz_localize(None)
    index = index_kline.sort_values("timestamp").rename(columns={"close": "index_close"}).copy()
    index["timestamp"] = index["timestamp"].dt.tz_localize(None)
    grid = pd.merge_asof(grid, mark, on="timestamp", direction="backward")
    grid = pd.merge_asof(grid, index, on="timestamp", direction="backward")
    grid["mark_close"] = grid["mark_close"].fillna(method="ffill")
    grid["index_close"] = grid["index_close"].fillna(method="ffill")

    grid["mark_basis"] = ((grid["mark_close"] - grid["index_close"]) / grid["index_close"].replace(0, pd.NA)).fillna(0.0)
    grid["mark_premium"] = grid["mark_basis"]
    grid["mark_premium_mean_4"] = grid["mark_premium"].rolling(4, min_periods=1).mean().fillna(0.0)
    grid["funding_rate_abs"] = grid["funding_rate"].abs()
    grid["funding_rate_mean_4"] = grid["funding_rate"].rolling(4, min_periods=1).mean().fillna(0.0)
    grid["funding_rate_std_4"] = grid["funding_rate"].rolling(4, min_periods=1).std().fillna(0.0)
    grid["futures_pressure"] = (grid["mark_premium"] - grid["funding_rate"]).fillna(0.0)

    return grid[["timestamp"] + FUNDING_FEATURE_COLUMNS]


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


def fetch_all_expanded(days: int, exchange: str, cache_dir: Path) -> tuple[dict, dict, dict, bool]:
    """
    Phase 1 of the expanded pipeline, factored out so both this script and
    tools/recalibrate_thresholds.py can reuse it against the same cache.
    Returns (raw_ohlcv, base_feats, funding_tables, all_caught_up).
    """
    since_ms = int((pd.Timestamp.utcnow() - pd.Timedelta(days=days)).timestamp() * 1000)
    raw_ohlcv: dict[str, pd.DataFrame] = {}
    base_feats: dict[str, pd.DataFrame] = {}
    funding_tables: dict[str, pd.DataFrame] = {}
    all_caught_up = True

    for symbol in ASSETS:
        print(f"\n=== [fetch] {symbol} OHLCV ===")
        raw = cached_fetch_ohlcv(exchange, symbol, since_ms, cache_dir, budget_s=38.0)
        caught_up = raw["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=30) if len(raw) else False
        all_caught_up = all_caught_up and caught_up
        print(f"  {len(raw)} candles ({raw['timestamp'].min()} .. {raw['timestamp'].max()})")
        raw_ohlcv[symbol] = raw
        if not len(raw):
            continue

        candles = [
            {"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for ts, o, h, l, c, v in zip(raw["timestamp"], raw["open"], raw["high"],
                                          raw["low"], raw["close"], raw["volume"])
        ]
        feats = build_features(candles).rename(columns={"date": "timestamp"})
        base_feats[symbol] = feats

        print(f"=== [fetch] {symbol} funding history + mark/index klines ===")
        funding_hist = cached_fetch_funding_history(symbol, since_ms, cache_dir, budget_s=12.0)
        mark_kline = cached_fetch_price_kline(symbol, "mark", since_ms, cache_dir, budget_s=25.0)
        index_kline = cached_fetch_price_kline(symbol, "index", since_ms, cache_dir, budget_s=25.0)
        print(f"  funding readings={len(funding_hist)}, mark_kline={len(mark_kline)}, index_kline={len(index_kline)}")
        if len(funding_hist) and len(mark_kline) and len(index_kline):
            funding_tables[symbol] = compute_funding_feature_table(
                feats["timestamp"], funding_hist, mark_kline, index_kline
            )
        else:
            print(f"  WARNING: incomplete funding/mark/index data for {symbol} — rerun --fetch-only to continue.")

    return raw_ohlcv, base_feats, funding_tables, all_caught_up


def assemble_expanded_table(symbol: str, base_feats: dict, funding_tables: dict) -> tuple[pd.DataFrame, list[str]]:
    """
    Phase 2's per-asset assembly, also factored out for reuse by
    tools/recalibrate_thresholds.py. Merges symbol's own base features with
    its funding table and its two peers' cross-asset columns. Returns
    (feats, feature_columns) — feature_columns is the exact 57-column list
    (or fewer if this asset's own base_feats aren't available, in which
    case the caller should skip it).
    """
    feats = base_feats[symbol].copy()

    if symbol in funding_tables:
        feats = feats.merge(funding_tables[symbol], on="timestamp", how="left")
        for col in FUNDING_FEATURE_COLUMNS:
            feats[col] = feats[col].fillna(0.0)
    else:
        for col in FUNDING_FEATURE_COLUMNS:
            feats[col] = 0.0
        print(f"  WARNING: {symbol} funding columns zero-filled for TRAINING only (no live data available "
              f"at retrain time) — the live gate will still correctly block on missing funding_ctx if the "
              f"backend can't reach Bybit; this only affects historical training rows.")

    for prefix in peer_prefixes_for(symbol):
        peer_symbol = next(s for s in ASSETS if s.split("/")[0].lower() == prefix)
        if peer_symbol not in base_feats:
            for suf in PEER_FEATURE_SUFFIXES:
                feats[f"{prefix}_{suf}"] = 0.0
            print(f"  WARNING: peer {peer_symbol} unavailable — {prefix}_* columns zero-filled for TRAINING only.")
            continue
        peer_feats = base_feats[peer_symbol][["timestamp", "log_return_1", "log_return_3",
                                                "trend_direction", "volume_block_strength"]].rename(columns={
            "log_return_1": f"{prefix}_return_1",
            "log_return_3": f"{prefix}_return_3",
            "trend_direction": f"{prefix}_trend",
            "volume_block_strength": f"{prefix}_volume_block",
        })
        feats = feats.merge(peer_feats, on="timestamp", how="left")
        for suf in PEER_FEATURE_SUFFIXES:
            feats[f"{prefix}_{suf}"] = feats[f"{prefix}_{suf}"].fillna(0.0)

    return feats, feature_columns_for(symbol)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--exchange", default="bybit")
    parser.add_argument("--models-dir", default=str(REPO_ROOT / "models"))
    parser.add_argument("--cache-dir", default="/tmp/ohlcv_cache")
    parser.add_argument("--no-expand", action="store_true",
                         help="Train only the base 41 live-computable columns (old H-13 behavior), skip funding/cross-asset.")
    parser.add_argument("--fetch-only", action="store_true",
                         help="Only fetch/cache OHLCV+funding+mark/index klines, skip training (for resumable multi-call fetching).")
    args = parser.parse_args()

    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)
    results = {}

    if args.no_expand:
        since_ms = int((pd.Timestamp.utcnow() - pd.Timedelta(days=args.days)).timestamp() * 1000)
        raw_ohlcv = {}
        all_caught_up = True
        for symbol in ASSETS:
            print(f"\n=== {symbol} ===")
            raw = cached_fetch_ohlcv(args.exchange, symbol, since_ms, cache_dir, budget_s=38.0)
            caught_up = raw["timestamp"].max() >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=30) if len(raw) else False
            all_caught_up = all_caught_up and caught_up
            print(f"  using {len(raw)} candles ({raw['timestamp'].min()} .. {raw['timestamp'].max()})")
            raw_ohlcv[symbol] = raw
        base_feats, funding_tables = {}, {}
    else:
        raw_ohlcv, base_feats, funding_tables, all_caught_up = fetch_all_expanded(args.days, args.exchange, cache_dir)

    if args.fetch_only:
        print(f"\n{'ALL CAUGHT UP' if all_caught_up else 'NOT CAUGHT UP YET — rerun with --fetch-only to continue'}")
        return 0

    # ── Phase 2: assemble the full per-asset feature table (base + funding
    # + cross-asset peers), label, and train. ──────────────────────────────
    for symbol, cfg in ASSETS.items():
        print(f"\n=== [train] {symbol} ===")

        if args.no_expand:
            raw = raw_ohlcv[symbol]
            if raw.empty:
                print(f"  SKIPPED — no OHLCV for {symbol}.")
                continue
            candles = [
                {"time": int(ts.timestamp()), "open": o, "high": h, "low": l, "close": c, "volume": v}
                for ts, o, h, l, c, v in zip(raw["timestamp"], raw["open"], raw["high"],
                                              raw["low"], raw["close"], raw["volume"])
            ]
            feats = build_features(candles).rename(columns={"date": "timestamp"})
            feature_columns = BASE_LIVE_FEATURE_COLUMNS
        else:
            if symbol not in base_feats:
                print(f"  SKIPPED — no base features available (OHLCV fetch incomplete for {symbol}).")
                continue
            feats, feature_columns = assemble_expanded_table(symbol, base_feats, funding_tables)

        missing = [c for c in feature_columns if c not in feats.columns]
        if missing:
            raise RuntimeError(f"Assembled feature table for {symbol} is missing expected columns: {missing}")
        print(f"  training on {len(feature_columns)} features "
              f"({'expanded: 41 base + 8 funding + 8 cross-asset' if not args.no_expand else 'base 41 only'})")

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
                X = d[feature_columns].astype(float)
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
                "feature_count": len(feature_columns),
                "entry_threshold": entry_thr, "exit_threshold": exit_thr,
                "valid_metrics": valid_metrics,
                "test_auc": test_auc, "test_precision": test_precision,
                "test_fire_rate": test_fire_rate,
                "train_rows": len(train_df), "valid_rows": len(valid_df), "test_rows": len(test_df),
            }

    out_meta = models_dir / "retrain_live_features_report.json"
    out_meta.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"\nWrote {out_meta}")
    print(f"{'(all data was CAUGHT UP)' if all_caught_up else '(WARNING: not all assets were fully caught up — see per-asset cache status above)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
