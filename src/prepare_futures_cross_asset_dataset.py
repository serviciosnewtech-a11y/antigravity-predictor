#!/usr/bin/env python3
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
from lgbm_poc.features import build_cross_asset_context
from lgbm_poc.futures import build_futures_market_frame, load_timeframe_frame
from lgbm_poc.io import write_parquet
from lgbm_poc.labels import label_tp_before_sl_1h


def _load_bundle(candle_path: str, mark_path: str | None, funding_path: str | None, rule: str) -> object:
    candles = load_timeframe_frame(candle_path)
    mark = load_timeframe_frame(mark_path) if mark_path else None
    funding = load_timeframe_frame(funding_path) if funding_path else None
    return build_futures_market_frame(candles, mark=mark, funding=funding, resample_rule=rule)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a BTC dataset with ETH/SOL futures context.")
    parser.add_argument("--btc-candles", required=True)
    parser.add_argument("--btc-mark", default=None)
    parser.add_argument("--btc-funding-rate", default=None)
    parser.add_argument("--eth-candles", required=True)
    parser.add_argument("--eth-mark", default=None)
    parser.add_argument("--eth-funding-rate", default=None)
    parser.add_argument("--sol-candles", required=True)
    parser.add_argument("--sol-mark", default=None)
    parser.add_argument("--sol-funding-rate", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument("--resample-rule", default="15min")
    parser.add_argument("--horizon-bars", type=int, default=4)
    parser.add_argument("--tp-atr-mult", type=float, default=1.5)
    parser.add_argument("--sl-atr-mult", type=float, default=1.0)
    args = parser.parse_args()

    btc = _load_bundle(args.btc_candles, args.btc_mark, args.btc_funding_rate, args.resample_rule)
    eth = _load_bundle(args.eth_candles, args.eth_mark, args.eth_funding_rate, args.resample_rule)
    sol = _load_bundle(args.sol_candles, args.sol_mark, args.sol_funding_rate, args.resample_rule)

    feats = build_cross_asset_context(btc, eth, sol)
    labeled = label_tp_before_sl_1h(feats, horizon_bars=args.horizon_bars, tp_atr_mult=args.tp_atr_mult, sl_atr_mult=args.sl_atr_mult)
    labeled = labeled.dropna(subset=["label_tp_before_sl_1h"]).reset_index(drop=True)
    write_parquet(labeled, args.output)
    print(f"wrote {len(labeled)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
