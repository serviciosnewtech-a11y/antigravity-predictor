#!/usr/bin/env python3
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
from lgbm_poc.features import build_cross_asset_context
from lgbm_poc.io import read_parquet, write_parquet
from lgbm_poc.labels import label_tp_before_sl_1h


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a BTC dataset with ETH/SOL cross-asset context.")
    parser.add_argument("--btc", required=True, help="BTC OHLCV parquet file.")
    parser.add_argument("--eth", required=True, help="ETH OHLCV parquet file.")
    parser.add_argument("--sol", required=True, help="SOL OHLCV parquet file.")
    parser.add_argument("--output", required=True, help="Output parquet dataset.")
    parser.add_argument("--horizon-bars", type=int, default=4)
    parser.add_argument("--tp-pct", type=float, default=0.003)
    parser.add_argument("--sl-pct", type=float, default=0.002)
    args = parser.parse_args()

    btc = read_parquet(args.btc)
    eth = read_parquet(args.eth)
    sol = read_parquet(args.sol)

    feats = build_cross_asset_context(btc, eth, sol)
    labeled = label_tp_before_sl_1h(feats, horizon_bars=args.horizon_bars, tp_pct=args.tp_pct, sl_pct=args.sl_pct)
    labeled = labeled.dropna(subset=["label_tp_before_sl_1h"]).reset_index(drop=True)
    write_parquet(labeled, args.output)
    print(f"wrote {len(labeled)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
